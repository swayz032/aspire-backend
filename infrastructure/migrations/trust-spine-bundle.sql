-- Aspire Trust Spine Bundle: 42 core + 7 A2A = 49 migrations
-- Generated: 2026-02-10
-- IMPORTANT: Execute as superuser (postgres role)

-- Supabase installs pgcrypto in the 'extensions' schema.
-- This ensures gen_random_bytes(), digest(), etc. are accessible without schema-qualifying.
SET search_path TO public, extensions;

-- ========== Migration: 20260105000100_tenancy_schema.sql ==========
-- Tenancy schema
create table if not exists tenants (
  tenant_id text primary key,
  name text not null,
  created_at timestamptz not null default now()
);

create table if not exists tenant_memberships (
  tenant_id text not null references tenants(tenant_id) on delete cascade,
  user_id uuid not null,
  role text not null check (role in ('owner','admin','member')),
  created_at timestamptz not null default now(),
  primary key (tenant_id, user_id)
);

create index if not exists idx_tenant_memberships_user on tenant_memberships (user_id);
create index if not exists idx_tenant_memberships_role on tenant_memberships (tenant_id, role);

-- ========== Migration: 20260105000200_tenancy_helpers.sql ==========
-- NOTE: auth schema is managed by Supabase — helper functions placed in app schema instead.
CREATE SCHEMA IF NOT EXISTS app;

create or replace function app.is_member(p_tenant_id text)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from tenant_memberships m
    where m.tenant_id = p_tenant_id and m.user_id = auth.uid()
  );
$$;

create or replace function app.is_admin_or_owner(p_tenant_id text)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from tenant_memberships m
    where m.tenant_id = p_tenant_id and m.user_id = auth.uid() and m.role in ('owner','admin')
  );
$$;

create or replace function app.is_owner(p_tenant_id text)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from tenant_memberships m
    where m.tenant_id = p_tenant_id and m.user_id = auth.uid() and m.role = 'owner'
  );
$$;

-- ========== Migration: 20260105000300_tenancy_rls.sql ==========
alter table tenants enable row level security;
alter table tenant_memberships enable row level security;

drop policy if exists tenants_select_members on tenants;
create policy tenants_select_members on tenants for select to authenticated
using (app.is_member(tenant_id));

drop policy if exists tenants_update_admin on tenants;
create policy tenants_update_admin on tenants for update to authenticated
using (app.is_admin_or_owner(tenant_id))
with check (app.is_admin_or_owner(tenant_id));

drop policy if exists tenants_no_insert on tenants;
create policy tenants_no_insert on tenants for insert to authenticated with check (false);

drop policy if exists tenants_no_delete on tenants;
create policy tenants_no_delete on tenants for delete to authenticated using (false);

drop policy if exists memberships_select_members on tenant_memberships;
create policy memberships_select_members on tenant_memberships for select to authenticated
using (app.is_member(tenant_id));

drop policy if exists memberships_insert_admin on tenant_memberships;
create policy memberships_insert_admin on tenant_memberships for insert to authenticated
with check (app.is_admin_or_owner(tenant_id));

drop policy if exists memberships_update_admin on tenant_memberships;
create policy memberships_update_admin on tenant_memberships for update to authenticated
using (app.is_admin_or_owner(tenant_id))
with check (app.is_admin_or_owner(tenant_id));

drop policy if exists memberships_delete_admin on tenant_memberships;
create policy memberships_delete_admin on tenant_memberships for delete to authenticated
using (app.is_admin_or_owner(tenant_id));

-- ========== Migration: 20260105000400_tenancy_triggers.sql ==========
create or replace function public.enforce_last_owner()
returns trigger
language plpgsql
as $$
declare owner_count int;
begin
  if (tg_op = 'DELETE') then
    if old.role = 'owner' then
      select count(*) into owner_count
      from tenant_memberships
      where tenant_id = old.tenant_id and role='owner' and user_id <> old.user_id;
      if owner_count = 0 then
        raise exception 'cannot remove last owner of tenant %', old.tenant_id;
      end if;
    end if;
    return old;
  end if;

  if (tg_op = 'UPDATE') then
    if old.role='owner' and new.role <> 'owner' then
      select count(*) into owner_count
      from tenant_memberships
      where tenant_id = old.tenant_id and role='owner' and user_id <> old.user_id;
      if owner_count = 0 then
        raise exception 'cannot demote last owner of tenant %', old.tenant_id;
      end if;
    end if;
    return new;
  end if;

  return new;
end;
$$;

drop trigger if exists trg_enforce_last_owner on tenant_memberships;
create trigger trg_enforce_last_owner
before update or delete on tenant_memberships
for each row execute function public.enforce_last_owner();

-- ========== Migration: 20260105002000_approvals_schema.sql ==========
create table if not exists approval_requests (
  approval_id text primary key,
  tenant_id text not null references tenants(tenant_id) on delete cascade,
  run_id text not null,
  trace_id text,
  orchestrator text,
  tool text not null,
  operation text not null,
  resource_type text,
  resource_id text,
  risk_tier text not null check (risk_tier in ('green','yellow','red')),
  policy_version text not null,
  approval_hash text not null,
  payload_redacted jsonb not null default '{}'::jsonb,
  constraints jsonb not null default '{}'::jsonb,
  status text not null check (status in ('pending','approved','rejected','expired','canceled')) default 'pending',
  created_by_user_id uuid,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  decided_at timestamptz,
  decided_by_user_id uuid,
  decision_surface text,
  decision_reason text
);

create index if not exists idx_approvals_tenant_status on approval_requests (tenant_id, status, expires_at);
create index if not exists idx_approvals_tenant_run on approval_requests (tenant_id, run_id);
create index if not exists idx_approvals_tenant_created_at on approval_requests (tenant_id, created_at desc);

-- ========== Migration: 20260105002100_approvals_triggers.sql ==========
create or replace function public.enforce_approval_immutability()
returns trigger
language plpgsql
as $$
begin
  if (tg_op = 'UPDATE') then
    if new.tenant_id <> old.tenant_id
      or new.run_id <> old.run_id
      or new.tool <> old.tool
      or new.operation <> old.operation
      or coalesce(new.resource_type,'') <> coalesce(old.resource_type,'')
      or coalesce(new.resource_id,'') <> coalesce(old.resource_id,'')
      or new.risk_tier <> old.risk_tier
      or new.policy_version <> old.policy_version
      or new.approval_hash <> old.approval_hash
      or new.payload_redacted <> old.payload_redacted
      or new.constraints <> old.constraints
      or new.created_at <> old.created_at
      or new.expires_at <> old.expires_at
      or coalesce(new.created_by_user_id::text,'') <> coalesce(old.created_by_user_id::text,'')
    then
      raise exception 'approval request immutable fields cannot be modified';
    end if;
  end if;
  return new;
end;
$$;

create or replace function public.enforce_approval_state_transitions()
returns trigger
language plpgsql
as $$
begin
  if (tg_op = 'UPDATE') then
    if old.status in ('approved','rejected','expired','canceled') and new.status <> old.status then
      raise exception 'cannot transition from terminal status %', old.status;
    end if;

    if old.status = 'pending' then
      if new.status not in ('pending','approved','rejected','expired','canceled') then
        raise exception 'invalid approval status transition';
      end if;
    end if;

    if new.status = 'approved' then
      if new.decided_at is null or new.decided_by_user_id is null then
        raise exception 'approved requires decided_at and decided_by_user_id';
      end if;
    end if;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_approval_immutability on approval_requests;
create trigger trg_approval_immutability
before update on approval_requests
for each row execute function public.enforce_approval_immutability();

drop trigger if exists trg_approval_state_transitions on approval_requests;
create trigger trg_approval_state_transitions
before update on approval_requests
for each row execute function public.enforce_approval_state_transitions();

-- ========== Migration: 20260105002200_approvals_rls.sql ==========
alter table approval_requests enable row level security;

drop policy if exists approvals_select_members on approval_requests;
create policy approvals_select_members on approval_requests for select to authenticated
using (app.is_member(tenant_id));

drop policy if exists approvals_insert_members on approval_requests;
create policy approvals_insert_members on approval_requests for insert to authenticated
with check (app.is_member(tenant_id));

drop policy if exists approvals_update_admin on approval_requests;
create policy approvals_update_admin on approval_requests for update to authenticated
using (app.is_admin_or_owner(tenant_id))
with check (app.is_admin_or_owner(tenant_id));

drop policy if exists approvals_no_delete on approval_requests;
create policy approvals_no_delete on approval_requests for delete to authenticated
using (false);

-- ========== Migration: 20260105002300_approvals_rpcs.sql ==========
-- RPCs for approval workflow

create or replace function rpc_create_approval_request(
  p_approval_id text,
  p_tenant_id text,
  p_run_id text,
  p_trace_id text,
  p_orchestrator text,
  p_tool text,
  p_operation text,
  p_resource_type text,
  p_resource_id text,
  p_risk_tier text,
  p_policy_version text,
  p_approval_hash text,
  p_payload_redacted jsonb,
  p_constraints jsonb,
  p_expires_in_seconds int
)
returns approval_requests
language plpgsql
security definer
set search_path = public
as $$
declare row approval_requests;
begin
  if p_risk_tier not in ('green','yellow','red') then
    raise exception 'invalid risk_tier';
  end if;
  if p_expires_in_seconds is null or p_expires_in_seconds <= 0 then
    raise exception 'invalid expiry';
  end if;

  insert into approval_requests (
    approval_id, tenant_id, run_id, trace_id, orchestrator,
    tool, operation, resource_type, resource_id,
    risk_tier, policy_version,
    approval_hash, payload_redacted, constraints,
    status, created_by_user_id, created_at, expires_at
  )
  values (
    p_approval_id, p_tenant_id, p_run_id, nullif(p_trace_id,''), nullif(p_orchestrator,''),
    p_tool, p_operation, nullif(p_resource_type,''), nullif(p_resource_id,''),
    p_risk_tier, p_policy_version,
    p_approval_hash, coalesce(p_payload_redacted,'{}'::jsonb), coalesce(p_constraints,'{}'::jsonb),
    'pending', auth.uid(), now(), now() + make_interval(secs => p_expires_in_seconds)
  )
  returning * into row;

  return row;
end;
$$;

create or replace function rpc_approve_request(
  p_approval_id text,
  p_approval_hash text,
  p_surface text
)
returns approval_requests
language plpgsql
security definer
set search_path = public
as $$
declare row approval_requests;
begin
  select * into row from approval_requests where approval_id = p_approval_id for update;
  if not found then raise exception 'approval not found'; end if;
  if row.status <> 'pending' then raise exception 'approval not pending'; end if;

  if row.expires_at <= now() then
    update approval_requests set status='expired' where approval_id=p_approval_id;
    raise exception 'approval expired';
  end if;

  if row.approval_hash <> p_approval_hash then
    raise exception 'approval hash mismatch';
  end if;

  update approval_requests
  set status='approved',
      decided_at=now(),
      decided_by_user_id=auth.uid(),
      decision_surface=nullif(p_surface,'')
  where approval_id=p_approval_id
  returning * into row;

  return row;
end;
$$;

create or replace function rpc_reject_request(
  p_approval_id text,
  p_reason text
)
returns approval_requests
language plpgsql
security definer
set search_path = public
as $$
declare row approval_requests;
begin
  select * into row from approval_requests where approval_id=p_approval_id for update;
  if not found then raise exception 'approval not found'; end if;
  if row.status <> 'pending' then raise exception 'approval not pending'; end if;

  update approval_requests
  set status='rejected',
      decided_at=now(),
      decided_by_user_id=auth.uid(),
      decision_reason=nullif(p_reason,'')
  where approval_id=p_approval_id
  returning * into row;

  return row;
end;
$$;

create or replace function rpc_expire_pending_approvals(
  p_tenant_id text default null,
  p_limit int default 500
)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare n int;
begin
  with c as (
    select approval_id
    from approval_requests
    where status='pending' and expires_at <= now()
      and (p_tenant_id is null or tenant_id = p_tenant_id)
    order by expires_at asc
    limit greatest(1, least(p_limit, 5000))
  )
  update approval_requests a
  set status='expired'
  from c
  where a.approval_id = c.approval_id;

  get diagnostics n = row_count;
  return n;
end;
$$;

create or replace function rpc_cancel_pending_approvals_for_run(
  p_tenant_id text,
  p_run_id text,
  p_reason text
)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare n int;
begin
  update approval_requests
  set status='canceled',
      decided_at=now(),
      decided_by_user_id=auth.uid(),
      decision_reason=nullif(p_reason,'')
  where tenant_id=p_tenant_id and run_id=p_run_id and status='pending';

  get diagnostics n = row_count;
  return n;
end;
$$;

-- ========== Migration: 20260105006000_provider_call_log_schema.sql ==========
create table if not exists provider_call_log (
  call_id text primary key,
  tenant_id text not null references tenants(tenant_id) on delete cascade,
  run_id text not null,
  trace_id text,
  user_id uuid,

  capability_jti text,
  receipt_id text,
  approval_id text,
  approval_hash text,

  tool text not null,
  operation text not null,
  resource_type text,
  resource_id text,

  external_provider text not null,
  external_request_id text,
  external_object_id text,

  params_hash text not null,
  request_summary jsonb not null default '{}'::jsonb,

  started_at timestamptz not null default now(),
  completed_at timestamptz,
  status text check (status in ('success','failed','partial','canceled')),
  http_status int,
  error_code text,
  error_detail text,

  created_at timestamptz not null default now()
);

create index if not exists idx_pcl_tenant_time on provider_call_log (tenant_id, started_at desc);
create index if not exists idx_pcl_tenant_run on provider_call_log (tenant_id, run_id);

-- ========== Migration: 20260105006100_provider_call_log_rls.sql ==========
alter table provider_call_log enable row level security;

drop policy if exists pcl_select_admin on provider_call_log;
create policy pcl_select_admin on provider_call_log for select to authenticated
using (app.is_admin_or_owner(tenant_id));

drop policy if exists pcl_no_insert_authenticated on provider_call_log;
create policy pcl_no_insert_authenticated on provider_call_log for insert to authenticated with check (false);

drop policy if exists pcl_no_update_authenticated on provider_call_log;
create policy pcl_no_update_authenticated on provider_call_log for update to authenticated using (false);

drop policy if exists pcl_no_delete_authenticated on provider_call_log;
create policy pcl_no_delete_authenticated on provider_call_log for delete to authenticated using (false);

-- ========== Migration: 20260105006200_provider_call_log_rpcs.sql ==========
create or replace function rpc_provider_call_start(
  p_call_id text, p_tenant_id text, p_run_id text, p_trace_id text, p_user_id uuid,
  p_capability_jti text, p_receipt_id text, p_approval_id text, p_approval_hash text,
  p_tool text, p_operation text, p_resource_type text, p_resource_id text,
  p_external_provider text, p_external_request_id text, p_params_hash text, p_request_summary jsonb
)
returns boolean
language plpgsql
security definer
set search_path=public
as $$
begin
  insert into provider_call_log (
    call_id, tenant_id, run_id, trace_id, user_id,
    capability_jti, receipt_id, approval_id, approval_hash,
    tool, operation, resource_type, resource_id,
    external_provider, external_request_id, params_hash, request_summary
  ) values (
    p_call_id, p_tenant_id, p_run_id, nullif(p_trace_id,''), p_user_id,
    nullif(p_capability_jti,''), nullif(p_receipt_id,''), nullif(p_approval_id,''), nullif(p_approval_hash,''),
    p_tool, p_operation, nullif(p_resource_type,''), nullif(p_resource_id,''),
    p_external_provider, nullif(p_external_request_id,''), p_params_hash, coalesce(p_request_summary,'{}'::jsonb)
  );
  return true;
end;
$$;

create or replace function rpc_provider_call_finish(
  p_call_id text, p_external_object_id text, p_external_request_id text,
  p_status text, p_http_status int, p_error_code text, p_error_detail text
)
returns boolean
language plpgsql
security definer
set search_path=public
as $$
begin
  update provider_call_log
  set completed_at=now(),
      external_object_id=nullif(p_external_object_id,''),
      external_request_id=coalesce(nullif(p_external_request_id,''), external_request_id),
      status=p_status,
      http_status=p_http_status,
      error_code=nullif(p_error_code,''),
      error_detail=nullif(p_error_detail,'')
  where call_id=p_call_id;
  return found;
end;
$$;

-- ========== Migration: 20260106006000_suite_office_identity.sql ==========
-- Aspire Option B: suite_id + office_id are canonical.
-- tenant_id remains as a legacy/compat alias (used by existing membership/RLS functions).

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS app;

-- Canonical suites (tenants)
CREATE TABLE IF NOT EXISTS app.suites (
  suite_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text UNIQUE,
  name text,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- If tenant_id is omitted, default to suite_id::text (keeps legacy systems working).
CREATE OR REPLACE FUNCTION app.tg_suite_default_tenant_id()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.tenant_id IS NULL OR btrim(NEW.tenant_id) = '' THEN
    NEW.tenant_id := NEW.suite_id::text;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_suite_default_tenant_id ON app.suites;
CREATE TRIGGER trg_suite_default_tenant_id
BEFORE INSERT ON app.suites
FOR EACH ROW
EXECUTE FUNCTION app.tg_suite_default_tenant_id();

CREATE INDEX IF NOT EXISTS idx_app_suites_tenant_id ON app.suites(tenant_id);

-- Canonical offices (seats / departments)
CREATE TABLE IF NOT EXISTS app.offices (
  office_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id uuid NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  label text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_app_offices_suite ON app.offices(suite_id);

-- Mapping helpers
CREATE OR REPLACE FUNCTION app.suite_tenant_id(p_suite_id uuid)
RETURNS text
LANGUAGE sql
STABLE
SET search_path = app
AS $$
  SELECT tenant_id FROM app.suites WHERE suite_id = p_suite_id
$$;

REVOKE ALL ON FUNCTION app.suite_tenant_id(uuid) FROM public;

CREATE OR REPLACE FUNCTION app.tenant_suite_id(p_tenant_id text)
RETURNS uuid
LANGUAGE sql
STABLE
SET search_path = app
AS $$
  SELECT suite_id FROM app.suites WHERE tenant_id = p_tenant_id
$$;

REVOKE ALL ON FUNCTION app.tenant_suite_id(text) FROM public;

-- Ensure suite exists for a given tenant_id (legacy entrypoint)
CREATE OR REPLACE FUNCTION app.ensure_suite(p_tenant_id text, p_name text DEFAULT NULL)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = app, public
AS $$
DECLARE
  v_suite_id uuid;
BEGIN
  IF p_tenant_id IS NULL OR btrim(p_tenant_id) = '' THEN
    RAISE EXCEPTION 'tenant_id required';
  END IF;

  SELECT suite_id INTO v_suite_id FROM app.suites WHERE tenant_id = p_tenant_id;
  IF v_suite_id IS NOT NULL THEN
    IF p_name IS NOT NULL THEN
      UPDATE app.suites SET name = COALESCE(name, p_name) WHERE suite_id = v_suite_id;
    END IF;
    RETURN v_suite_id;
  END IF;

  INSERT INTO app.suites(tenant_id, name)
  VALUES (p_tenant_id, p_name)
  RETURNING suite_id INTO v_suite_id;

  -- Optional: keep a legacy tenants table populated if it exists.
  IF to_regclass('public.tenants') IS NOT NULL THEN
    INSERT INTO public.tenants(tenant_id, name)
    VALUES (p_tenant_id, COALESCE(p_name, p_tenant_id))
    ON CONFLICT (tenant_id) DO NOTHING;
  END IF;

  RETURN v_suite_id;
END;
$$;

REVOKE ALL ON FUNCTION app.ensure_suite(text, text) FROM public;

-- Generic trigger helper to keep tenant_id synced from suite_id for legacy RLS.
CREATE OR REPLACE FUNCTION public.trust_sync_tenant_id_from_suite()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = app, public
AS $$
DECLARE
  v_tenant_id text;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    -- prevent suite_id drift
    IF NEW.suite_id IS DISTINCT FROM OLD.suite_id THEN
      RAISE EXCEPTION 'suite_id is immutable';
    END IF;
  END IF;

  SELECT tenant_id INTO v_tenant_id FROM app.suites WHERE suite_id = NEW.suite_id;
  IF v_tenant_id IS NULL OR btrim(v_tenant_id) = '' THEN
    RAISE EXCEPTION 'unknown suite_id';
  END IF;

  NEW.tenant_id := v_tenant_id;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.trust_sync_tenant_id_from_suite() FROM public;


-- Grants for PostgREST/Edge Functions
GRANT USAGE ON SCHEMA app TO authenticated, service_role;
GRANT SELECT ON app.suites TO authenticated, service_role;
GRANT SELECT ON app.offices TO authenticated, service_role;
COMMIT;

-- ========== Migration: 20260106006100_suite_tenant_sync.sql ==========
-- Keep legacy tenancy table in sync with canonical Suite identity.
-- This reduces drift when code writes directly to app.suites.

create or replace function app.trust_sync_tenant_from_suite()
returns trigger
language plpgsql
security definer
set search_path = public, app
as $$
begin
  -- Upsert into legacy tenants table for compatibility (RLS helpers, approvals, etc.)
  insert into public.tenants (tenant_id, name)
  values (new.tenant_id, coalesce(new.name, 'unnamed-suite'))
  on conflict (tenant_id) do update
    set name = excluded.name;

  return new;
end;
$$;

drop trigger if exists trg_suite_sync_tenant on app.suites;
create trigger trg_suite_sync_tenant
after insert or update on app.suites
for each row execute function app.trust_sync_tenant_from_suite();

-- ========== Migration: 20260106007000_inbox_schema.sql ==========
-- Inbox schema (Option B)
-- Canonical: suite_id (uuid) + office_id (uuid)
-- Legacy/compat: tenant_id is auto-synced from suite_id (for existing membership/RLS)

CREATE TABLE IF NOT EXISTS inbox_items (
  id text PRIMARY KEY,
  suite_id uuid NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  office_id uuid NULL REFERENCES app.offices(office_id) ON DELETE SET NULL,
  type text NOT NULL CHECK (type in ('MAIL','CALL','OFFICE','TASK')),
  title text NOT NULL,
  preview text,
  priority text NOT NULL CHECK (priority in ('LOW','NORMAL','HIGH','URGENT')),
  status text NOT NULL CHECK (status in ('NEW','OPEN','WAITING','DONE','ARCHIVED')) DEFAULT 'NEW',
  assigned_to uuid,
  unread boolean NOT NULL DEFAULT true,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inbox_suite_status ON inbox_items(suite_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_suite_unread ON inbox_items(suite_id, unread, updated_at DESC);
-- Legacy helper indexes
CREATE INDEX IF NOT EXISTS idx_inbox_tenant_status ON inbox_items(tenant_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_tenant_unread ON inbox_items(tenant_id, unread, updated_at DESC);

CREATE OR REPLACE FUNCTION public.set_updated_at_inbox_items()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_inbox_items_updated_at ON inbox_items;
CREATE TRIGGER trg_inbox_items_updated_at
BEFORE UPDATE ON inbox_items
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at_inbox_items();

-- Keep tenant_id synced from suite_id
DROP TRIGGER IF EXISTS trg_inbox_sync_tenant_id ON inbox_items;
CREATE TRIGGER trg_inbox_sync_tenant_id
BEFORE INSERT OR UPDATE ON inbox_items
FOR EACH ROW EXECUTE FUNCTION public.trust_sync_tenant_id_from_suite();

-- ========== Migration: 20260106007100_inbox_rls.sql ==========
alter table inbox_items enable row level security;

-- Members can read their tenant inbox
drop policy if exists inbox_items_select on inbox_items;
create policy inbox_items_select on inbox_items
  for select
  using (app.is_member(tenant_id));

-- Members can insert ingress items only for their tenant
drop policy if exists inbox_items_insert on inbox_items;
create policy inbox_items_insert on inbox_items
  for insert
  with check (app.is_member(tenant_id));

-- Admin/owner can update assignment/status
drop policy if exists inbox_items_update on inbox_items;
create policy inbox_items_update on inbox_items
  for update
  using (app.is_admin_or_owner(tenant_id))
  with check (app.is_admin_or_owner(tenant_id));

-- Nobody deletes from inbox_items (archive via status)
revoke delete on inbox_items from anon, authenticated;

-- ========== Migration: 20260106007200_outbox_schema.sql ==========
-- Outbox schema (Option B)
-- Canonical: suite_id (uuid)
-- Legacy/compat: tenant_id is auto-synced from suite_id

CREATE TABLE IF NOT EXISTS outbox_jobs (
  id text PRIMARY KEY,
  suite_id uuid NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  action_type text NOT NULL,
  idempotency_key text NOT NULL,
  status text NOT NULL CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','DEAD')) DEFAULT 'QUEUED',
  attempt_count integer NOT NULL DEFAULT 0,
  not_before timestamptz,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_error text,
  locked_at timestamptz,
  locked_by text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (suite_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_outbox_suite_status ON outbox_jobs(suite_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox_jobs(status, not_before);
-- Legacy index
CREATE INDEX IF NOT EXISTS idx_outbox_tenant_status ON outbox_jobs(tenant_id, status, updated_at DESC);

CREATE OR REPLACE FUNCTION public.set_updated_at_outbox_jobs()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_outbox_jobs_updated_at ON outbox_jobs;
CREATE TRIGGER trg_outbox_jobs_updated_at
BEFORE UPDATE ON outbox_jobs
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at_outbox_jobs();

-- Keep tenant_id consistent with suite_id
DROP TRIGGER IF EXISTS trg_outbox_jobs_sync_tenant ON outbox_jobs;
CREATE TRIGGER trg_outbox_jobs_sync_tenant
BEFORE INSERT OR UPDATE ON outbox_jobs
FOR EACH ROW EXECUTE FUNCTION public.trust_sync_tenant_id_from_suite();

-- ========== Migration: 20260106007300_outbox_rls.sql ==========
alter table outbox_jobs enable row level security;

-- Members can read their tenant outbox
drop policy if exists outbox_jobs_select on outbox_jobs;
create policy outbox_jobs_select on outbox_jobs
  for select
  using (app.is_member(tenant_id));

-- Admin/owner can enqueue jobs for their tenant
drop policy if exists outbox_jobs_insert on outbox_jobs;
create policy outbox_jobs_insert on outbox_jobs
  for insert
  with check (app.is_admin_or_owner(tenant_id));

-- Updates are reserved for server/worker. Use RPCs (security definer) and/or service role.
drop policy if exists outbox_jobs_update on outbox_jobs;
create policy outbox_jobs_update on outbox_jobs
  for update
  using (false);

-- ========== Migration: 20260106007400_outbox_rpcs.sql ==========
-- Atomic claim for outbox worker (prevents double execution)
-- Option B: suite_id is canonical; tenant_id is derived.
-- Intended to be called by a trusted worker (service role) or admin user.

CREATE OR REPLACE FUNCTION public.claim_outbox_jobs(p_suite_id uuid, p_limit int DEFAULT 10, p_worker_id text DEFAULT 'worker')
RETURNS SETOF outbox_jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
BEGIN
  -- If called with user JWT, require admin/owner membership (legacy check uses tenant_id)
  IF auth.uid() IS NOT NULL THEN
    IF NOT app.is_admin_or_owner(app.suite_tenant_id(p_suite_id)) THEN
      RAISE EXCEPTION 'not authorized to claim outbox jobs';
    END IF;
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT id
    FROM outbox_jobs
    WHERE suite_id = p_suite_id
      AND status = 'QUEUED'
      AND (not_before IS NULL OR not_before <= now())
    ORDER BY created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  )
  UPDATE outbox_jobs j
    SET status = 'RUNNING',
        locked_at = now(),
        locked_by = p_worker_id,
        attempt_count = j.attempt_count + 1
  FROM candidate c
  WHERE j.id = c.id
  RETURNING j.*;
END;
$$;

REVOKE ALL ON FUNCTION public.claim_outbox_jobs(uuid,int,text) FROM public;

-- ========== Migration: 20260106007500_approval_events_schema.sql ==========
-- Approval events schema (Option B)
-- Canonical: suite_id
-- Legacy/compat: tenant_id auto-synced (approval_requests remains tenant_id keyed)

CREATE TABLE IF NOT EXISTS approval_events (
  id text PRIMARY KEY,
  suite_id uuid NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  approval_id text NOT NULL REFERENCES approval_requests(approval_id) ON DELETE CASCADE,
  actor_user_id uuid,
  event_type text NOT NULL CHECK (event_type IN ('CREATED','APPROVED','REJECTED','EDITED')),
  reason_code text,
  draft_artifact jsonb,
  final_artifact jsonb,
  diff jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_events_approval ON approval_events(approval_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_approval_events_suite ON approval_events(suite_id, created_at DESC);
-- Legacy index
CREATE INDEX IF NOT EXISTS idx_approval_events_tenant ON approval_events(tenant_id, created_at DESC);

-- Keep tenant_id consistent with suite_id
DROP TRIGGER IF EXISTS trg_approval_events_sync_tenant ON approval_events;
CREATE TRIGGER trg_approval_events_sync_tenant
BEFORE INSERT OR UPDATE ON approval_events
FOR EACH ROW EXECUTE FUNCTION public.trust_sync_tenant_id_from_suite();

-- ========== Migration: 20260106007600_approval_events_rls.sql ==========
alter table approval_events enable row level security;

-- Members can read events for approvals in their tenant
drop policy if exists approval_events_select on approval_events;
create policy approval_events_select on approval_events
  for select
  using (app.is_member(tenant_id));

-- Members can insert EDITED events (draft changes) for their tenant.
-- APPROVED/REJECTED events should typically be written by server-side approval RPCs.
drop policy if exists approval_events_insert_member on approval_events;
create policy approval_events_insert_member on approval_events
  for insert
  with check (
    app.is_member(tenant_id)
    and event_type in ('EDITED','CREATED')
  );

-- ========== Migration: 20260116008000_trust_immutability.sql ==========
-- Trust Spine hardening: Append-only enforcement
-- Blocks UPDATE/DELETE for core audit tables so the database enforces immutability.
-- Safe to re-run.

create or replace function trust_deny_mutation() returns trigger as $$
begin
  raise exception 'append-only table: mutation is not allowed';
end;
$$ language plpgsql;

create or replace function trust_attach_immutability(p_table regclass) returns void as $$
begin
  execute format('drop trigger if exists trg_immutability_ud on %s;', p_table);
  execute format(
    'create trigger trg_immutability_ud before update or delete on %s for each row execute function trust_deny_mutation();',
    p_table
  );
end;
$$ language plpgsql;

-- Attach to known audit tables (only those that exist).
do $$
begin
  if to_regclass('public.receipts') is not null then
    perform trust_attach_immutability('public.receipts');
  end if;
  if to_regclass('public.approval_events') is not null then
    perform trust_attach_immutability('public.approval_events');
  end if;
  if to_regclass('public.provider_call_log') is not null then
    perform trust_attach_immutability('public.provider_call_log');
  end if;
end;
$$;

-- ========== Migration: 20260116008100_trust_idempotency.sql ==========
-- Trust Spine hardening: Idempotency constraints
-- Safe to re-run.

-- Outbox: do not allow two jobs with the same idempotency_key per tenant.
do $$
begin
  if to_regclass('public.outbox_jobs') is not null then
    begin
      alter table public.outbox_jobs
        add constraint outbox_jobs_tenant_idempotency_uniq unique (tenant_id, idempotency_key);
    exception when duplicate_object or duplicate_table then
      null;  -- catch both constraint (42710) and backing index (42P07)
    end;
  end if;
end;
$$;

-- Receipts: optional secondary guard (only if you store idempotency_key on receipts).
do $$
begin
  if to_regclass('public.receipts') is not null then
    if exists (
      select 1 from information_schema.columns
      where table_schema='public' and table_name='receipts' and column_name='idempotency_key'
    ) then
      create unique index if not exists idx_receipts_tenant_idempotency
        on public.receipts(tenant_id, idempotency_key)
        where idempotency_key is not null;
    end if;
  end if;
end;
$$;

-- ========== Migration: 20260116008200_trust_pii_redaction.sql ==========
-- Trust Spine hardening: PII/Secret redaction baseline
-- Goal: reduce accidental secret storage in DB logs.
-- Scope: shallow redaction (top-level keys). Pair with application-layer deep redaction.

create or replace function trust_redact_shallow(p_obj jsonb)
returns jsonb
language sql
immutable
as $$
  select coalesce(
    (
      select jsonb_object_agg(
        key,
        case
          when lower(key) in (
            'authorization','auth','token','access_token','refresh_token','id_token',
            'api_key','apikey','secret','password','passphrase','client_secret',
            'private_key','secret_key','webhook_secret','signature'
          ) then to_jsonb('[REDACTED]'::text)
          else value
        end
      )
      from jsonb_each(coalesce(p_obj, '{}'::jsonb))
    ),
    '{}'::jsonb
  );
$$;

-- Provider call log redaction
-- Supports both legacy `request_summary` usage and optional `request_redacted/response_redacted` payload columns.

create or replace function trust_provider_call_log_redact_trigger()
returns trigger
language plpgsql
as $$
begin
  -- Baseline schema field
  if new.request_summary is not null then
    new.request_summary := trust_redact_shallow(new.request_summary);
  end if;

  -- Optional payload fields (added by this migration if missing)
  if new.request_redacted is not null then
    new.request_redacted := trust_redact_shallow(new.request_redacted);
  end if;
  if new.response_redacted is not null then
    new.response_redacted := trust_redact_shallow(new.response_redacted);
  end if;

  return new;
end;
$$;

-- Attach to provider_call_log if it exists.
do $$
begin
  if to_regclass('public.provider_call_log') is not null then
    -- Add optional columns safely (for executor/adapter payload storage)
    begin
      alter table public.provider_call_log add column if not exists request_redacted jsonb;
      alter table public.provider_call_log add column if not exists response_redacted jsonb;
    exception when others then
      -- If the table exists but is managed elsewhere, don't block the migration.
      null;
    end;

    drop trigger if exists trg_provider_call_log_redact on public.provider_call_log;
    create trigger trg_provider_call_log_redact
      before insert or update on public.provider_call_log
      for each row execute function trust_provider_call_log_redact_trigger();
  end if;
end;
$$;

-- ========== Migration: 20260116008300_receipts_schema.sql ==========
-- Receipts schema (Option B scaffold)
-- Canonical: suite_id (uuid) + office_id (uuid)
-- Legacy/compat: tenant_id auto-synced from suite_id

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS receipts (
  receipt_id text PRIMARY KEY DEFAULT encode(gen_random_bytes(16), 'hex'),
  suite_id uuid NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  office_id uuid,
  receipt_type text NOT NULL,
  status text NOT NULL CHECK (status IN ('PENDING','SUCCEEDED','FAILED','DENIED')) DEFAULT 'PENDING',
  correlation_id text NOT NULL,
  actor_type text NOT NULL CHECK (actor_type IN ('USER','SYSTEM','WORKER')) DEFAULT 'SYSTEM',
  actor_id text,
  action jsonb NOT NULL DEFAULT '{}'::jsonb,
  result jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  hash_alg text NOT NULL DEFAULT 'sha256',
  receipt_hash bytea,
  signature text
);

CREATE INDEX IF NOT EXISTS idx_receipts_suite_created ON receipts(suite_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_receipts_tenant_created ON receipts(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_receipts_corr ON receipts(correlation_id);

CREATE TABLE IF NOT EXISTS receipt_items (
  id bigserial PRIMARY KEY,
  receipt_id text NOT NULL REFERENCES receipts(receipt_id) ON DELETE CASCADE,
  seq int NOT NULL,
  item jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (receipt_id, seq)
);

-- Keep tenant_id consistent with suite_id
DROP TRIGGER IF EXISTS trg_receipts_sync_tenant ON receipts;
CREATE TRIGGER trg_receipts_sync_tenant
BEFORE INSERT OR UPDATE ON receipts
FOR EACH ROW EXECUTE FUNCTION public.trust_sync_tenant_id_from_suite();

-- Hash helpers (payload includes suite_id + tenant_id)
CREATE OR REPLACE FUNCTION public.trust_compute_receipt_hash(p_receipt_id text)
RETURNS bytea
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_payload text;
  v_hash bytea;
BEGIN
  SELECT (
    r.suite_id::text || '|' || r.tenant_id || '|' || r.receipt_type || '|' || r.status || '|' || r.correlation_id || '|' ||
    COALESCE(r.actor_type,'') || '|' || COALESCE(r.actor_id,'') || '|' ||
    COALESCE(r.office_id::text,'') || '|' ||
    COALESCE(r.action::text,'{}') || '|' || COALESCE(r.result::text,'{}') || '|' ||
    r.created_at::text
  ) INTO v_payload
  FROM receipts r
  WHERE r.receipt_id = p_receipt_id;

  IF v_payload IS NULL THEN
    RAISE EXCEPTION 'receipt not found: %', p_receipt_id;
  END IF;

  v_hash := digest(convert_to(v_payload, 'utf8'), 'sha256');
  RETURN v_hash;
END;
$$;

REVOKE ALL ON FUNCTION public.trust_compute_receipt_hash(text) FROM public;

-- Enforce append-only semantics: allow ONLY setting signature after insert.
CREATE OR REPLACE FUNCTION public.trust_receipts_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF (OLD.signature IS NULL AND NEW.signature IS NOT NULL)
       AND (NEW.suite_id = OLD.suite_id)
       AND (NEW.tenant_id = OLD.tenant_id)
       AND (COALESCE(NEW.office_id::text,'') = COALESCE(OLD.office_id::text,''))
       AND (NEW.receipt_type = OLD.receipt_type)
       AND (NEW.status = OLD.status)
       AND (NEW.correlation_id = OLD.correlation_id)
       AND (COALESCE(NEW.actor_type,'') = COALESCE(OLD.actor_type,''))
       AND (COALESCE(NEW.actor_id,'') = COALESCE(OLD.actor_id,''))
       AND (NEW.action = OLD.action)
       AND (NEW.result = OLD.result)
       AND (NEW.created_at = OLD.created_at)
    THEN
      RETURN NEW;
    END IF;

    RAISE EXCEPTION 'receipts are append-only';
  END IF;

  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'receipts cannot be deleted';
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_receipts_immutable ON receipts;
CREATE TRIGGER trg_receipts_immutable
BEFORE UPDATE OR DELETE ON receipts
FOR EACH ROW EXECUTE FUNCTION public.trust_receipts_immutable();

-- ========== Migration: 20260116008350_receipts_rls.sql ==========
-- Receipts RLS (Option B): tenant isolation for the simplified receipts table.
-- Read: members of tenant
-- Write: members (service role bypasses RLS by design)

alter table receipts enable row level security;

-- Members can read receipts for their tenant
drop policy if exists receipts_select_members on receipts;
create policy receipts_select_members on receipts
  for select
  using (app.is_member(tenant_id));

-- Members can insert receipts for their tenant
drop policy if exists receipts_insert_members on receipts;
create policy receipts_insert_members on receipts
  for insert
  with check (app.is_member(tenant_id));

-- No updates/deletes (append-only)
drop policy if exists receipts_no_update on receipts;
create policy receipts_no_update on receipts
  for update
  using (false)
  with check (false);

drop policy if exists receipts_no_delete on receipts;
create policy receipts_no_delete on receipts
  for delete
  using (false);

-- ========== Migration: 20260116008400_policy_schema.sql ==========
-- Policy Engine schema (vNext scaffold)

create table if not exists policy_versions (
  id bigserial primary key,
  tenant_id text not null references tenants(tenant_id) on delete cascade,
  version text not null,
  is_active boolean not null default false,
  created_at timestamptz not null default now(),
  unique (tenant_id, version)
);

create table if not exists policy_rules (
  id bigserial primary key,
  policy_version_id bigint not null references policy_versions(id) on delete cascade,
  rule_key text not null,
  effect text not null check (effect in ('ALLOW','DENY','REQUIRE_APPROVAL')),
  priority int not null default 100,
  match jsonb not null default '{}'::jsonb,
  reason_code text not null default 'POLICY_RULE',
  created_at timestamptz not null default now(),
  unique (policy_version_id, rule_key)
);

create table if not exists policy_decisions (
  decision_id text primary key default encode(gen_random_bytes(16), 'hex'),
  tenant_id text not null references tenants(tenant_id) on delete cascade,
  correlation_id text not null,
  subject jsonb not null default '{}'::jsonb,
  action jsonb not null default '{}'::jsonb,
  decision text not null check (decision in ('ALLOW','DENY','REQUIRE_APPROVAL')),
  reason_code text not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_policy_decisions_tenant_created on policy_decisions(tenant_id, created_at desc);

-- Minimal deterministic evaluator:
-- - matches only on action.tool equality (action->>'tool')
-- - takes the first matching rule by priority (lower wins)
create or replace function public.trust_policy_eval(p_tenant_id text, p_subject jsonb, p_action jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_tool text;
  v_decision text := 'ALLOW';
  v_reason text := 'DEFAULT_ALLOW';
begin
  v_tool := p_action->>'tool';

  select r.effect, r.reason_code
    into v_decision, v_reason
  from policy_versions v
  join policy_rules r on r.policy_version_id = v.id
  where v.tenant_id = p_tenant_id
    and v.is_active = true
    and (r.match->>'tool') is not null
    and (r.match->>'tool') = v_tool
  order by r.priority asc
  limit 1;

  insert into policy_decisions (tenant_id, correlation_id, subject, action, decision, reason_code)
  values (p_tenant_id, coalesce(p_action->>'correlation_id',''), p_subject, p_action, v_decision, v_reason);

  return jsonb_build_object(
    'decision', v_decision,
    'reason_code', v_reason,
    'tool', v_tool
  );
end;
$$;

revoke all on function public.trust_policy_eval(text,jsonb,jsonb) from public;

-- ========== Migration: 20260116008500_executor_schema.sql ==========
-- Executor hardening tables + RPCs

create table if not exists outbox_dead_letters (
  id bigserial primary key,
  job_id text not null,
  tenant_id text not null,
  action_type text,
  idempotency_key text,
  payload jsonb not null default '{}'::jsonb,
  last_error text,
  failed_at timestamptz not null default now(),
  unique (job_id)
);

-- NOTE:
-- provider_call_log is defined in the baseline prerequisite migration
-- `20260105006000_provider_call_log_schema.sql`.
-- Executor adapters should log redacted request/response into
-- `provider_call_log.request_redacted/response_redacted` (added by redaction hardening).

create or replace function public.complete_outbox_job(p_job_id text)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update outbox_jobs
     set status = 'SUCCEEDED',
         last_error = null,
         locked_at = null,
         locked_by = null
   where id = p_job_id;
end;
$$;

create or replace function public.fail_outbox_job(p_job_id text, p_error text)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v outbox_jobs;
begin
  update outbox_jobs
     set status = case when attempt_count >= 5 then 'DEAD' else 'FAILED' end,
         last_error = p_error,
         locked_at = null,
         locked_by = null
   where id = p_job_id
   returning * into v;

  insert into outbox_dead_letters (job_id, tenant_id, action_type, idempotency_key, payload, last_error)
  values (v.id, v.tenant_id, v.action_type, v.idempotency_key, v.payload, v.last_error)
  on conflict (job_id) do nothing;
end;
$$;

revoke all on function public.complete_outbox_job(text) from public;
revoke all on function public.fail_outbox_job(text,text) from public;

-- ========== Migration: 20260116008600_certification_schema.sql ==========
-- Certification scaffolding for Skill Packs

create table if not exists skill_pack_manifests (
  manifest_id text primary key default encode(gen_random_bytes(16), 'hex'),
  tenant_id text not null references tenants(tenant_id) on delete cascade,
  pack_id text not null,
  manifest jsonb not null,
  created_at timestamptz not null default now(),
  unique (tenant_id, pack_id)
);

create table if not exists certification_runs (
  run_id text primary key default encode(gen_random_bytes(16), 'hex'),
  tenant_id text not null references tenants(tenant_id) on delete cascade,
  pack_id text not null,
  status text not null check (status in ('RUNNING','PASSED','FAILED')) default 'RUNNING',
  started_at timestamptz not null default now(),
  finished_at timestamptz
);

create table if not exists certification_results (
  result_id text primary key default encode(gen_random_bytes(16), 'hex'),
  run_id text not null references certification_runs(run_id) on delete cascade,
  check_key text not null,
  passed boolean not null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (run_id, check_key)
);

-- ========== Migration: 20260116008700_receipts_crypto.sql ==========
-- Receipts crypto + hash enforcement (Option B)
-- Aligns with receipts table including suite_id + tenant_id + office_id.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Compute and set receipt_hash on INSERT (deterministic; uses row fields only).
CREATE OR REPLACE FUNCTION public.trust_set_receipt_hash()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_payload text;
BEGIN
  IF NEW.receipt_id IS NULL OR NEW.receipt_id = '' THEN
    NEW.receipt_id := encode(gen_random_bytes(16), 'hex');
  END IF;

  IF NEW.created_at IS NULL THEN
    NEW.created_at := now();
  END IF;

  -- Ensure tenant_id is available for deterministic hashing (legacy alias)
  IF NEW.tenant_id IS NULL OR btrim(NEW.tenant_id) = '' THEN
    NEW.tenant_id := app.suite_tenant_id(NEW.suite_id);
  END IF;

  IF NEW.receipt_hash IS NULL THEN
    v_payload := (
      NEW.suite_id::text || '|' || NEW.tenant_id || '|' || NEW.receipt_type || '|' || NEW.status || '|' || NEW.correlation_id || '|' ||
      COALESCE(NEW.actor_type,'') || '|' || COALESCE(NEW.actor_id,'') || '|' ||
      COALESCE(NEW.office_id::text,'') || '|' ||
      COALESCE(NEW.action::text,'{}') || '|' || COALESCE(NEW.result::text,'{}') || '|' ||
      NEW.created_at::text
    );

    NEW.receipt_hash := digest(convert_to(v_payload, 'utf8'), 'sha256');
  END IF;

  RETURN NEW;
END;
$$;

-- Ensure trigger uses the latest hash setter.
DROP TRIGGER IF EXISTS trg_receipts_hash ON receipts;
CREATE TRIGGER trg_receipts_hash
BEFORE INSERT ON receipts
FOR EACH ROW
EXECUTE FUNCTION public.trust_set_receipt_hash();

-- Verify a stored hash equals the recomputed one.
CREATE OR REPLACE FUNCTION public.trust_verify_receipt_hash(p_receipt_id text)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_expected bytea;
  v_current bytea;
BEGIN
  SELECT receipt_hash INTO v_current FROM receipts WHERE receipt_id = p_receipt_id;
  IF v_current IS NULL THEN
    RETURN false;
  END IF;

  v_expected := public.trust_compute_receipt_hash(p_receipt_id);
  RETURN v_expected IS NOT DISTINCT FROM v_current;
END;
$$;

REVOKE ALL ON FUNCTION public.trust_verify_receipt_hash(text) FROM public;

-- ========== Migration: 20260116008800_capability_tokens_schema.sql ==========
-- Capability tokens (v1)
-- Purpose: bind high-risk actions to explicit, expiring capabilities that must be consumed
-- before execution. Tokens are stored hashed; plaintext is returned only at issuance.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS capability_tokens (
  token_id text PRIMARY KEY DEFAULT encode(gen_random_bytes(16), 'hex'),
  suite_id uuid NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  office_id uuid,
  correlation_id text NOT NULL,
  scope text NOT NULL,
  requested_action jsonb NOT NULL DEFAULT '{}'::jsonb,
  token_hash bytea NOT NULL,
  issued_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  used_by_user_id uuid,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (token_hash)
);

CREATE INDEX IF NOT EXISTS idx_cap_tokens_suite_created ON capability_tokens(suite_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_cap_tokens_tenant_created ON capability_tokens(tenant_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_cap_tokens_corr ON capability_tokens(correlation_id);
CREATE INDEX IF NOT EXISTS idx_cap_tokens_expires ON capability_tokens(expires_at);

-- Keep tenant_id consistent with suite_id
DROP TRIGGER IF EXISTS trg_cap_tokens_sync_tenant ON capability_tokens;
CREATE TRIGGER trg_cap_tokens_sync_tenant
BEFORE INSERT OR UPDATE ON capability_tokens
FOR EACH ROW EXECUTE FUNCTION public.trust_sync_tenant_id_from_suite();

-- Issue a capability token (returns plaintext token exactly once)
CREATE OR REPLACE FUNCTION public.trust_issue_capability_token(
  p_suite_id uuid,
  p_office_id uuid,
  p_scope text,
  p_ttl_seconds int,
  p_correlation_id text,
  p_requested_action jsonb DEFAULT '{}'::jsonb,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_token text;
  v_hash bytea;
  v_expires timestamptz;
  v_token_id text;
BEGIN
  IF p_ttl_seconds IS NULL OR p_ttl_seconds <= 0 OR p_ttl_seconds > 86400 THEN
    RAISE EXCEPTION 'ttl_seconds must be between 1 and 86400';
  END IF;

  -- Only owners/admins can issue from a user context; service-role bypasses RLS.
  IF auth.uid() IS NOT NULL THEN
    IF NOT app.is_admin_or_owner((p_suite_id::text)) THEN
      RAISE EXCEPTION 'not authorized to issue capability token';
    END IF;
  END IF;

  v_token := encode(gen_random_bytes(32), 'hex');
  v_hash := digest(convert_to(v_token, 'utf8'), 'sha256');
  v_expires := now() + make_interval(secs => p_ttl_seconds);

  INSERT INTO capability_tokens (suite_id, tenant_id, office_id, correlation_id, scope, requested_action, token_hash, expires_at, metadata)
  VALUES (p_suite_id, p_suite_id::text, p_office_id, p_correlation_id, p_scope, COALESCE(p_requested_action,'{}'::jsonb), v_hash, v_expires, COALESCE(p_metadata,'{}'::jsonb))
  RETURNING token_id INTO v_token_id;

  RETURN jsonb_build_object(
    'token_id', v_token_id,
    'token', v_token,
    'expires_at', v_expires,
    'scope', p_scope,
    'correlation_id', p_correlation_id
  );
END;
$$;

REVOKE ALL ON FUNCTION public.trust_issue_capability_token(uuid,uuid,text,int,text,jsonb,jsonb) FROM public;

-- Consume a capability token (single-use)
CREATE OR REPLACE FUNCTION public.trust_consume_capability_token(
  p_token text,
  p_expected_scope text,
  p_expected_correlation_id text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_hash bytea;
  v_row capability_tokens;
BEGIN
  IF p_token IS NULL OR length(p_token) < 16 THEN
    RAISE EXCEPTION 'invalid token';
  END IF;
  v_hash := digest(convert_to(p_token, 'utf8'), 'sha256');

  UPDATE capability_tokens
     SET used_at = now(),
         used_by_user_id = auth.uid()
   WHERE token_hash = v_hash
     AND used_at IS NULL
     AND expires_at > now()
     AND scope = p_expected_scope
     AND (p_expected_correlation_id IS NULL OR correlation_id = p_expected_correlation_id)
   RETURNING * INTO v_row;

  IF v_row.token_id IS NULL THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'NOT_FOUND_OR_EXPIRED');
  END IF;

  RETURN jsonb_build_object(
    'ok', true,
    'token_id', v_row.token_id,
    'suite_id', v_row.suite_id,
    'tenant_id', v_row.tenant_id,
    'office_id', v_row.office_id,
    'scope', v_row.scope,
    'correlation_id', v_row.correlation_id,
    'requested_action', v_row.requested_action
  );
END;
$$;

REVOKE ALL ON FUNCTION public.trust_consume_capability_token(text,text,text) FROM public;

-- ========== Migration: 20260116008850_capability_tokens_rls.sql ==========
-- RLS for capability_tokens

ALTER TABLE capability_tokens ENABLE ROW LEVEL SECURITY;

-- Members can view tokens metadata (NO plaintext token is stored).
DROP POLICY IF EXISTS cap_tokens_select ON capability_tokens;
CREATE POLICY cap_tokens_select
ON capability_tokens
FOR SELECT
USING (app.is_member(tenant_id));

-- Only owner/admin can create tokens directly (recommended: issue via RPC + service role).
DROP POLICY IF EXISTS cap_tokens_insert ON capability_tokens;
CREATE POLICY cap_tokens_insert
ON capability_tokens
FOR INSERT
WITH CHECK (app.is_admin_or_owner(tenant_id));

-- Updates only permitted for owner/admin (service role bypasses RLS for executor).
DROP POLICY IF EXISTS cap_tokens_update ON capability_tokens;
CREATE POLICY cap_tokens_update
ON capability_tokens
FOR UPDATE
USING (app.is_admin_or_owner(tenant_id))
WITH CHECK (app.is_admin_or_owner(tenant_id));

-- Deletes are not allowed.
DROP POLICY IF EXISTS cap_tokens_delete ON capability_tokens;
CREATE POLICY cap_tokens_delete
ON capability_tokens
FOR DELETE
USING (false);

-- ========== Migration: 20260116008900_execution_controls_schema.sql ==========
-- Execution controls / kill switches (v1)
-- Allows per-suite provider disablement or forcing approvals-only mode.

CREATE TABLE IF NOT EXISTS execution_controls (
  suite_id uuid NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  provider text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('ENABLED','APPROVAL_ONLY','DISABLED')) DEFAULT 'ENABLED',
  reason text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by_user_id uuid,
  PRIMARY KEY (suite_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_exec_controls_tenant ON execution_controls(tenant_id, provider);

-- Keep tenant_id consistent with suite_id
DROP TRIGGER IF EXISTS trg_exec_controls_sync_tenant ON execution_controls;
CREATE TRIGGER trg_exec_controls_sync_tenant
BEFORE INSERT OR UPDATE ON execution_controls
FOR EACH ROW EXECUTE FUNCTION public.trust_sync_tenant_id_from_suite();

CREATE OR REPLACE FUNCTION public.trust_exec_controls_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  NEW.updated_by_user_id = auth.uid();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_exec_controls_updated_at ON execution_controls;
CREATE TRIGGER trg_exec_controls_updated_at
BEFORE UPDATE ON execution_controls
FOR EACH ROW EXECUTE FUNCTION public.trust_exec_controls_updated_at();

-- Read helper for executors/routers
CREATE OR REPLACE FUNCTION public.trust_get_execution_mode(p_suite_id uuid, p_provider text)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT mode FROM execution_controls WHERE suite_id = p_suite_id AND provider = p_provider),
    'ENABLED'
  );
$$;

REVOKE ALL ON FUNCTION public.trust_get_execution_mode(uuid,text) FROM public;

-- ========== Migration: 20260116008950_execution_controls_rls.sql ==========
-- RLS for execution_controls

ALTER TABLE execution_controls ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS exec_controls_select ON execution_controls;
CREATE POLICY exec_controls_select
ON execution_controls
FOR SELECT
USING (app.is_member(tenant_id));

DROP POLICY IF EXISTS exec_controls_insert ON execution_controls;
CREATE POLICY exec_controls_insert
ON execution_controls
FOR INSERT
WITH CHECK (app.is_admin_or_owner(tenant_id));

DROP POLICY IF EXISTS exec_controls_update ON execution_controls;
CREATE POLICY exec_controls_update
ON execution_controls
FOR UPDATE
USING (app.is_admin_or_owner(tenant_id))
WITH CHECK (app.is_admin_or_owner(tenant_id));

DROP POLICY IF EXISTS exec_controls_delete ON execution_controls;
CREATE POLICY exec_controls_delete
ON execution_controls
FOR DELETE
USING (app.is_owner(tenant_id));

-- ========== Migration: 20260116009000_privileged_audit_log_schema.sql ==========
-- Privileged audit log (v1)
-- Records admin/owner changes to sensitive control-plane objects.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS privileged_audit_log (
  id bigserial PRIMARY KEY,
  suite_id uuid,
  tenant_id text,
  office_id uuid,
  actor_user_id uuid,
  actor_role text,
  action text NOT NULL,
  target_type text NOT NULL,
  target_id text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_created ON privileged_audit_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_suite_created ON privileged_audit_log(suite_id, created_at DESC);

-- Utility: best-effort cast text->uuid
CREATE OR REPLACE FUNCTION public.trust_try_uuid(p_text text)
RETURNS uuid
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  v uuid;
BEGIN
  BEGIN
    v := p_text::uuid;
  EXCEPTION WHEN others THEN
    v := NULL;
  END;
  RETURN v;
END;
$$;

REVOKE ALL ON FUNCTION public.trust_try_uuid(text) FROM public;

-- Insert helper (callable from triggers)
CREATE OR REPLACE FUNCTION public.trust_audit_log(
  p_tenant_id text,
  p_action text,
  p_target_type text,
  p_target_id text,
  p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_suite uuid;
  v_role text;
BEGIN
  v_suite := public.trust_try_uuid(p_tenant_id);

  SELECT role INTO v_role
  FROM tenant_memberships
  WHERE tenant_id = p_tenant_id AND user_id = auth.uid();

  INSERT INTO privileged_audit_log (suite_id, tenant_id, actor_user_id, actor_role, action, target_type, target_id, metadata)
  VALUES (v_suite, p_tenant_id, auth.uid(), v_role, p_action, p_target_type, p_target_id, COALESCE(p_metadata,'{}'::jsonb));
END;
$$;

REVOKE ALL ON FUNCTION public.trust_audit_log(text,text,text,text,jsonb) FROM public;

-- Auto-audit execution_controls changes
CREATE OR REPLACE FUNCTION public.trust_audit_execution_controls()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    PERFORM public.trust_audit_log(NEW.tenant_id, 'EXEC_CONTROLS_CREATE', 'execution_controls', NEW.provider,
      jsonb_build_object('mode', NEW.mode, 'reason', NEW.reason));
    RETURN NEW;
  ELSIF TG_OP = 'UPDATE' THEN
    PERFORM public.trust_audit_log(NEW.tenant_id, 'EXEC_CONTROLS_UPDATE', 'execution_controls', NEW.provider,
      jsonb_build_object('old_mode', OLD.mode, 'new_mode', NEW.mode, 'reason', NEW.reason));
    RETURN NEW;
  ELSIF TG_OP = 'DELETE' THEN
    PERFORM public.trust_audit_log(OLD.tenant_id, 'EXEC_CONTROLS_DELETE', 'execution_controls', OLD.provider,
      jsonb_build_object('old_mode', OLD.mode, 'reason', OLD.reason));
    RETURN OLD;
  END IF;
  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_exec_controls ON execution_controls;
CREATE TRIGGER trg_audit_exec_controls
AFTER INSERT OR UPDATE OR DELETE ON execution_controls
FOR EACH ROW EXECUTE FUNCTION public.trust_audit_execution_controls();

-- ========== Migration: 20260116009050_privileged_audit_log_rls.sql ==========
-- RLS for privileged_audit_log

ALTER TABLE privileged_audit_log ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_log_select ON privileged_audit_log;
CREATE POLICY audit_log_select
ON privileged_audit_log
FOR SELECT
USING (tenant_id IS NOT NULL AND app.is_admin_or_owner(tenant_id));

-- Inserts should come from triggers/service role; allowing admin/owner also keeps trigger behavior
-- safe even if SECURITY DEFINER semantics differ across environments.
DROP POLICY IF EXISTS audit_log_insert ON privileged_audit_log;
CREATE POLICY audit_log_insert
ON privileged_audit_log
FOR INSERT
WITH CHECK (tenant_id IS NOT NULL AND app.is_admin_or_owner(tenant_id));

DROP POLICY IF EXISTS audit_log_update ON privileged_audit_log;
CREATE POLICY audit_log_update
ON privileged_audit_log
FOR UPDATE
USING (false);

DROP POLICY IF EXISTS audit_log_delete ON privileged_audit_log;
CREATE POLICY audit_log_delete
ON privileged_audit_log
FOR DELETE
USING (false);

-- ========== Migration: 20260116009100_trace_context_columns.sql ==========
-- Trace context columns (v1)
-- Adds trace/run/span fields to key tables to enable end-to-end correlation.

-- A2A inbox
ALTER TABLE inbox_items
  ADD COLUMN IF NOT EXISTS trace_id text,
  ADD COLUMN IF NOT EXISTS span_id text,
  ADD COLUMN IF NOT EXISTS parent_span_id text,
  ADD COLUMN IF NOT EXISTS run_id text;

CREATE INDEX IF NOT EXISTS idx_inbox_trace ON inbox_items(trace_id);
CREATE INDEX IF NOT EXISTS idx_inbox_run ON inbox_items(run_id);

-- Outbox
ALTER TABLE outbox_jobs
  ADD COLUMN IF NOT EXISTS trace_id text,
  ADD COLUMN IF NOT EXISTS span_id text,
  ADD COLUMN IF NOT EXISTS parent_span_id text,
  ADD COLUMN IF NOT EXISTS run_id text;

CREATE INDEX IF NOT EXISTS idx_outbox_trace ON outbox_jobs(trace_id);
CREATE INDEX IF NOT EXISTS idx_outbox_run ON outbox_jobs(run_id);

-- Approval events
ALTER TABLE approval_events
  ADD COLUMN IF NOT EXISTS trace_id text,
  ADD COLUMN IF NOT EXISTS span_id text,
  ADD COLUMN IF NOT EXISTS parent_span_id text,
  ADD COLUMN IF NOT EXISTS run_id text;

CREATE INDEX IF NOT EXISTS idx_approval_events_trace ON approval_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_approval_events_run ON approval_events(run_id);

-- Provider call log
ALTER TABLE provider_call_log
  ADD COLUMN IF NOT EXISTS trace_id text,
  ADD COLUMN IF NOT EXISTS span_id text,
  ADD COLUMN IF NOT EXISTS parent_span_id text,
  ADD COLUMN IF NOT EXISTS run_id text;

CREATE INDEX IF NOT EXISTS idx_provider_log_trace ON provider_call_log(trace_id);
CREATE INDEX IF NOT EXISTS idx_provider_log_run ON provider_call_log(run_id);

-- Receipts
ALTER TABLE receipts
  ADD COLUMN IF NOT EXISTS trace_id text,
  ADD COLUMN IF NOT EXISTS span_id text,
  ADD COLUMN IF NOT EXISTS parent_span_id text,
  ADD COLUMN IF NOT EXISTS run_id text;

CREATE INDEX IF NOT EXISTS idx_receipts_trace ON receipts(trace_id);
CREATE INDEX IF NOT EXISTS idx_receipts_run ON receipts(run_id);

-- Approvals (extend existing)
ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS span_id text,
  ADD COLUMN IF NOT EXISTS parent_span_id text;

CREATE INDEX IF NOT EXISTS idx_approvals_trace ON approval_requests(trace_id);
CREATE INDEX IF NOT EXISTS idx_approvals_run2 ON approval_requests(run_id);

-- ========== Migration: 20260116009200_retention_jobs.sql ==========
-- Retention helpers (v1)
-- NOTE: Receipts are append-only and are intentionally NOT deleted by retention.
-- These helpers purge high-volume operational logs with tenant-scoped policies.

CREATE OR REPLACE FUNCTION public.trust_apply_retention(
  p_tenant_id text,
  p_provider_call_log_days int DEFAULT 30,
  p_outbox_dead_letter_days int DEFAULT 90,
  p_policy_decisions_days int DEFAULT 30
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_cut_provider timestamptz;
  v_cut_dlq timestamptz;
  v_cut_policy timestamptz;
  v_provider_deleted bigint;
  v_dlq_deleted bigint;
  v_policy_deleted bigint;
BEGIN
  v_cut_provider := now() - make_interval(days => p_provider_call_log_days);
  v_cut_dlq := now() - make_interval(days => p_outbox_dead_letter_days);
  v_cut_policy := now() - make_interval(days => p_policy_decisions_days);

  DELETE FROM provider_call_log
   WHERE tenant_id = p_tenant_id
     AND created_at < v_cut_provider;
  GET DIAGNOSTICS v_provider_deleted = ROW_COUNT;

  DELETE FROM outbox_dead_letters
   WHERE tenant_id = p_tenant_id
     AND failed_at < v_cut_dlq;
  GET DIAGNOSTICS v_dlq_deleted = ROW_COUNT;

  DELETE FROM policy_decisions
   WHERE tenant_id = p_tenant_id
     AND created_at < v_cut_policy;
  GET DIAGNOSTICS v_policy_deleted = ROW_COUNT;

  RETURN jsonb_build_object(
    'ok', true,
    'tenant_id', p_tenant_id,
    'provider_call_log_deleted', v_provider_deleted,
    'outbox_dead_letters_deleted', v_dlq_deleted,
    'policy_decisions_deleted', v_policy_deleted
  );
END;
$$;

REVOKE ALL ON FUNCTION public.trust_apply_retention(text,int,int,int) FROM public;

-- ========== Migration: 20260116009300_release_flags_schema.sql ==========
-- Release flags / feature flags (v1)
-- Used for canary rollouts, kill switches, and gradual feature enablement per suite.

CREATE TABLE IF NOT EXISTS release_flags (
  suite_id uuid NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  flag_key text NOT NULL,
  enabled boolean NOT NULL DEFAULT false,
  rollout_percent int NOT NULL DEFAULT 0 CHECK (rollout_percent >= 0 AND rollout_percent <= 100),
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by_user_id uuid,
  PRIMARY KEY (suite_id, flag_key)
);

CREATE INDEX IF NOT EXISTS idx_release_flags_tenant ON release_flags(tenant_id, flag_key);

DROP TRIGGER IF EXISTS trg_release_flags_sync_tenant ON release_flags;
CREATE TRIGGER trg_release_flags_sync_tenant
BEFORE INSERT OR UPDATE ON release_flags
FOR EACH ROW EXECUTE FUNCTION public.trust_sync_tenant_id_from_suite();

CREATE OR REPLACE FUNCTION public.trust_release_flags_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  NEW.updated_by_user_id = auth.uid();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_release_flags_updated_at ON release_flags;
CREATE TRIGGER trg_release_flags_updated_at
BEFORE UPDATE ON release_flags
FOR EACH ROW EXECUTE FUNCTION public.trust_release_flags_updated_at();

ALTER TABLE release_flags ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS release_flags_select ON release_flags;
CREATE POLICY release_flags_select
ON release_flags
FOR SELECT
USING (app.is_member(tenant_id));

DROP POLICY IF EXISTS release_flags_write ON release_flags;
CREATE POLICY release_flags_write
ON release_flags
FOR INSERT
WITH CHECK (app.is_admin_or_owner(tenant_id));

DROP POLICY IF EXISTS release_flags_update ON release_flags;
CREATE POLICY release_flags_update
ON release_flags
FOR UPDATE
USING (app.is_admin_or_owner(tenant_id))
WITH CHECK (app.is_admin_or_owner(tenant_id));

DROP POLICY IF EXISTS release_flags_delete ON release_flags;
CREATE POLICY release_flags_delete
ON release_flags
FOR DELETE
USING (app.is_owner(tenant_id));

-- Deterministic flag evaluation for server-side checks
CREATE OR REPLACE FUNCTION public.trust_is_flag_enabled(p_suite_id uuid, p_flag_key text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT enabled FROM release_flags WHERE suite_id = p_suite_id AND flag_key = p_flag_key),
    false
  );
$$;

REVOKE ALL ON FUNCTION public.trust_is_flag_enabled(uuid,text) FROM public;

-- ========== Migration: 20260116009400_policy_rls.sql ==========
-- RLS for policy engine tables

ALTER TABLE policy_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_decisions ENABLE ROW LEVEL SECURITY;

-- Read: members
DROP POLICY IF EXISTS policy_versions_select ON policy_versions;
CREATE POLICY policy_versions_select
ON policy_versions
FOR SELECT
USING (app.is_member(tenant_id));

DROP POLICY IF EXISTS policy_rules_select ON policy_rules;
CREATE POLICY policy_rules_select
ON policy_rules
FOR SELECT
USING (
  EXISTS (
    SELECT 1
    FROM policy_versions v
    WHERE v.id = policy_rules.policy_version_id
      AND app.is_member(v.tenant_id)
  )
);

DROP POLICY IF EXISTS policy_decisions_select ON policy_decisions;
CREATE POLICY policy_decisions_select
ON policy_decisions
FOR SELECT
USING (app.is_member(tenant_id));

-- Write: admin/owner
DROP POLICY IF EXISTS policy_versions_write ON policy_versions;
CREATE POLICY policy_versions_write
ON policy_versions
FOR INSERT
WITH CHECK (app.is_admin_or_owner(tenant_id));

DROP POLICY IF EXISTS policy_versions_update ON policy_versions;
CREATE POLICY policy_versions_update
ON policy_versions
FOR UPDATE
USING (app.is_admin_or_owner(tenant_id))
WITH CHECK (app.is_admin_or_owner(tenant_id));

DROP POLICY IF EXISTS policy_rules_write ON policy_rules;
CREATE POLICY policy_rules_write
ON policy_rules
FOR INSERT
WITH CHECK (
  EXISTS (
    SELECT 1 FROM policy_versions v
    WHERE v.id = policy_rules.policy_version_id
      AND app.is_admin_or_owner(v.tenant_id)
  )
);

DROP POLICY IF EXISTS policy_rules_update ON policy_rules;
CREATE POLICY policy_rules_update
ON policy_rules
FOR UPDATE
USING (
  EXISTS (
    SELECT 1 FROM policy_versions v
    WHERE v.id = policy_rules.policy_version_id
      AND app.is_admin_or_owner(v.tenant_id)
  )
)
WITH CHECK (
  EXISTS (
    SELECT 1 FROM policy_versions v
    WHERE v.id = policy_rules.policy_version_id
      AND app.is_admin_or_owner(v.tenant_id)
  )
);

-- Decisions: allow inserts (trust_policy_eval is SECURITY DEFINER; this is a safe fallback)
DROP POLICY IF EXISTS policy_decisions_insert ON policy_decisions;
CREATE POLICY policy_decisions_insert
ON policy_decisions
FOR INSERT
WITH CHECK (app.is_member(tenant_id));

-- Deletes disabled
DROP POLICY IF EXISTS policy_versions_delete ON policy_versions;
CREATE POLICY policy_versions_delete
ON policy_versions
FOR DELETE
USING (false);

DROP POLICY IF EXISTS policy_rules_delete ON policy_rules;
CREATE POLICY policy_rules_delete
ON policy_rules
FOR DELETE
USING (false);

DROP POLICY IF EXISTS policy_decisions_delete ON policy_decisions;
CREATE POLICY policy_decisions_delete
ON policy_decisions
FOR DELETE
USING (false);

-- ========== Migration: 20260201009500_presence_sessions_schema.sql ==========
-- Presence sessions (Ava video-required enforcement)
--
-- Purpose
-- -------
-- Server-side proof that a human operator is present on VIDEO during high-risk actions.
-- This is designed to be *enforced by the server* (Gateway + Trust Spine), not by UI.
--
-- Key properties
-- 1) Fail-closed: if there is no active presence session with VIDEO=ON, high-risk actions are blocked.
-- 2) Tamper-resistant: token is never stored, only a SHA-256 hash.
-- 3) No direct writes by clients: writes should go through SECURITY DEFINER RPCs (see 20260201009600).

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS presence_sessions (
  session_id text PRIMARY KEY,
  suite_id uuid NOT NULL REFERENCES app.suites(suite_id) ON DELETE CASCADE,
  tenant_id text NOT NULL,
  office_id uuid NULL REFERENCES app.offices(office_id) ON DELETE SET NULL,
  meeting_id text NULL,
  mode text NOT NULL CHECK (mode IN ('VIDEO_REQUIRED','VIDEO_OPTIONAL')) DEFAULT 'VIDEO_REQUIRED',
  provider text NULL,
  provider_session_id text NULL,
  token_hash bytea NOT NULL,
  video_state text NOT NULL CHECK (video_state IN ('ON','OFF','UNKNOWN')) DEFAULT 'UNKNOWN',
  last_presence_at timestamptz NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz NULL,
  ended_reason text NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_presence_suite_active
  ON presence_sessions(suite_id, ended_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_presence_suite_recent
  ON presence_sessions(suite_id, last_presence_at DESC);
CREATE INDEX IF NOT EXISTS idx_presence_tenant_recent
  ON presence_sessions(tenant_id, last_presence_at DESC);
CREATE INDEX IF NOT EXISTS idx_presence_office_active
  ON presence_sessions(office_id, ended_at) WHERE ended_at IS NULL;

-- updated_at trigger
CREATE OR REPLACE FUNCTION public.set_updated_at_presence_sessions()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_presence_sessions_updated_at ON presence_sessions;
CREATE TRIGGER trg_presence_sessions_updated_at
BEFORE UPDATE ON presence_sessions
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at_presence_sessions();

-- Keep tenant_id synced from suite_id (legacy membership/RLS functions use tenant_id)
DROP TRIGGER IF EXISTS trg_presence_sync_tenant_id ON presence_sessions;
CREATE TRIGGER trg_presence_sync_tenant_id
BEFORE INSERT OR UPDATE ON presence_sessions
FOR EACH ROW EXECUTE FUNCTION public.trust_sync_tenant_id_from_suite();

-- Prevent mismatch: office must belong to suite
CREATE OR REPLACE FUNCTION public.trust_assert_office_in_suite()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = app, public
AS $$
DECLARE
  v_office_suite uuid;
BEGIN
  IF NEW.office_id IS NULL THEN
    RETURN NEW;
  END IF;

  SELECT suite_id INTO v_office_suite
  FROM app.offices
  WHERE office_id = NEW.office_id;

  IF v_office_suite IS NULL THEN
    RAISE EXCEPTION 'unknown office_id';
  END IF;

  IF v_office_suite <> NEW.suite_id THEN
    RAISE EXCEPTION 'office_id does not belong to suite_id';
  END IF;

  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.trust_assert_office_in_suite() FROM public;

DROP TRIGGER IF EXISTS trg_presence_office_suite_check ON presence_sessions;
CREATE TRIGGER trg_presence_office_suite_check
BEFORE INSERT OR UPDATE ON presence_sessions
FOR EACH ROW EXECUTE FUNCTION public.trust_assert_office_in_suite();

-- Safe read surface (no token hash)
CREATE OR REPLACE VIEW public.presence_sessions_view AS
  SELECT
    session_id,
    suite_id,
    tenant_id,
    office_id,
    meeting_id,
    mode,
    provider,
    provider_session_id,
    video_state,
    last_presence_at,
    started_at,
    ended_at,
    ended_reason,
    metadata,
    created_at,
    updated_at
  FROM public.presence_sessions;

COMMIT;

-- ========== Migration: 20260201009550_presence_sessions_rls.sql ==========
-- Presence sessions RLS / grants
--
-- Default stance: clients should NOT be able to write directly to presence_sessions.
-- Writes happen via SECURITY DEFINER RPCs (see 20260201009600_presence_sessions_rpcs.sql).

ALTER TABLE public.presence_sessions ENABLE ROW LEVEL SECURITY;

-- Allow members to read presence sessions for their tenant (via the safe view).
DROP POLICY IF EXISTS presence_sessions_select_members ON public.presence_sessions;
CREATE POLICY presence_sessions_select_members ON public.presence_sessions
  FOR SELECT
  USING (app.is_member(tenant_id));

-- Deny all direct writes by authenticated users (fail closed).
DROP POLICY IF EXISTS presence_sessions_insert_deny ON public.presence_sessions;
CREATE POLICY presence_sessions_insert_deny ON public.presence_sessions
  FOR INSERT
  WITH CHECK (false);

DROP POLICY IF EXISTS presence_sessions_update_deny ON public.presence_sessions;
CREATE POLICY presence_sessions_update_deny ON public.presence_sessions
  FOR UPDATE
  USING (false)
  WITH CHECK (false);

DROP POLICY IF EXISTS presence_sessions_delete_deny ON public.presence_sessions;
CREATE POLICY presence_sessions_delete_deny ON public.presence_sessions
  FOR DELETE
  USING (false);

-- Privileges: no direct table access for anon/authenticated.
REVOKE ALL ON TABLE public.presence_sessions FROM anon, authenticated;
REVOKE ALL ON TABLE public.presence_sessions_view FROM anon;
GRANT SELECT ON TABLE public.presence_sessions_view TO authenticated;

-- ========== Migration: 20260201009600_presence_sessions_rpcs.sql ==========
-- Presence session RPCs
--
-- These are SECURITY DEFINER functions. In Supabase, ensure these functions are owned by a role
-- that can write presence_sessions even when RLS denies direct client writes (typically postgres/service_role).

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION app.start_presence_session(
  p_suite_id uuid,
  p_office_id uuid DEFAULT NULL,
  p_meeting_id text DEFAULT NULL,
  p_mode text DEFAULT 'VIDEO_REQUIRED'
)
RETURNS TABLE (
  session_id text,
  token text,
  suite_id uuid,
  office_id uuid,
  started_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = app, public
AS $$
DECLARE
  v_tenant_id text;
  v_session_id text;
  v_token text;
  v_mode text;
BEGIN
  SELECT tenant_id INTO v_tenant_id FROM app.suites WHERE suite_id = p_suite_id;
  IF v_tenant_id IS NULL OR btrim(v_tenant_id) = '' THEN
    RAISE EXCEPTION 'unknown suite_id';
  END IF;

  IF NOT app.is_member(v_tenant_id) THEN
    RAISE EXCEPTION 'not authorized';
  END IF;

  IF lower(p_mode) NOT IN ('video_required','video_optional') THEN
    RAISE EXCEPTION 'invalid mode (expected VIDEO_REQUIRED or VIDEO_OPTIONAL)';
  END IF;

  v_mode := CASE WHEN lower(p_mode) = 'video_optional' THEN 'VIDEO_OPTIONAL' ELSE 'VIDEO_REQUIRED' END;

  v_session_id := encode(gen_random_bytes(16), 'hex');
  v_token := encode(gen_random_bytes(32), 'hex');

  INSERT INTO public.presence_sessions(
    session_id,
    suite_id,
    tenant_id,
    office_id,
    meeting_id,
    mode,
    token_hash,
    video_state,
    last_presence_at
  ) VALUES (
    v_session_id,
    p_suite_id,
    v_tenant_id,
    p_office_id,
    p_meeting_id,
    v_mode,
    digest(v_token, 'sha256'),
    'UNKNOWN',
    now()
  );

  RETURN QUERY SELECT v_session_id, v_token, p_suite_id, p_office_id, now();
END;
$$;

REVOKE ALL ON FUNCTION app.start_presence_session(uuid, uuid, text, text) FROM public;

CREATE OR REPLACE FUNCTION app.presence_heartbeat(
  p_session_id text,
  p_token text,
  p_video_state text DEFAULT 'UNKNOWN'
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = app, public
AS $$
DECLARE
  v_tenant_id text;
  v_hash bytea;
  v_state text;
BEGIN
  v_hash := digest(COALESCE(p_token,''), 'sha256');
  v_state := CASE WHEN upper(p_video_state) IN ('ON','OFF') THEN upper(p_video_state) ELSE 'UNKNOWN' END;

  SELECT tenant_id INTO v_tenant_id
  FROM public.presence_sessions
  WHERE session_id = p_session_id AND ended_at IS NULL AND token_hash = v_hash;

  IF v_tenant_id IS NULL THEN
    RETURN false;
  END IF;

  IF NOT app.is_member(v_tenant_id) THEN
    RAISE EXCEPTION 'not authorized';
  END IF;

  UPDATE public.presence_sessions
  SET last_presence_at = now(),
      video_state = v_state,
      updated_at = now()
  WHERE session_id = p_session_id;

  RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION app.presence_heartbeat(text, text, text) FROM public;

CREATE OR REPLACE FUNCTION app.end_presence_session(
  p_session_id text,
  p_token text,
  p_reason text DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = app, public
AS $$
DECLARE
  v_tenant_id text;
  v_hash bytea;
BEGIN
  v_hash := digest(COALESCE(p_token,''), 'sha256');

  SELECT tenant_id INTO v_tenant_id
  FROM public.presence_sessions
  WHERE session_id = p_session_id AND ended_at IS NULL AND token_hash = v_hash;

  IF v_tenant_id IS NULL THEN
    RETURN false;
  END IF;

  IF NOT app.is_member(v_tenant_id) THEN
    RAISE EXCEPTION 'not authorized';
  END IF;

  UPDATE public.presence_sessions
  SET ended_at = now(),
      ended_reason = p_reason,
      updated_at = now()
  WHERE session_id = p_session_id;

  RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION app.end_presence_session(text, text, text) FROM public;

-- Read-side enforcement helper: is there a "recent" active session with VIDEO=ON?
CREATE OR REPLACE FUNCTION app.is_video_present(
  p_suite_id uuid,
  p_office_id uuid DEFAULT NULL,
  p_window_seconds integer DEFAULT 30
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = app, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.presence_sessions ps
    WHERE ps.suite_id = p_suite_id
      AND ps.ended_at IS NULL
      AND ps.video_state = 'ON'
      AND ps.last_presence_at IS NOT NULL
      AND ps.last_presence_at >= now() - make_interval(secs => GREATEST(1, p_window_seconds))
      AND (p_office_id IS NULL OR ps.office_id = p_office_id)
  );
$$;

REVOKE ALL ON FUNCTION app.is_video_present(uuid, uuid, integer) FROM public;

-- Grant execute to authenticated + service_role
GRANT EXECUTE ON FUNCTION app.start_presence_session(uuid, uuid, text, text) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION app.presence_heartbeat(text, text, text) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION app.end_presence_session(text, text, text) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION app.is_video_present(uuid, uuid, integer) TO authenticated, service_role;

COMMIT;

-- ========== Migration: 20260116020000_a2a_inbox_core.sql ==========
-- Trust Spine: Internal A2A Inbox MVP (core)
-- Canonical tenancy: suite_id UUID, created_by_office_id UUID

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- Optional helper: set suite context (prefer your existing roadmap helper if present)
CREATE OR REPLACE FUNCTION app.set_suite_context(p_suite_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);
END;
$$;

-- Enum types
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'a2a_task_status') THEN
    CREATE TYPE a2a_task_status AS ENUM (
      'created',
      'blocked',
      'claimed',
      'in_progress',
      'done',
      'failed',
      'quarantined',
      'canceled'
    );
  END IF;
END$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'a2a_task_event_type') THEN
    CREATE TYPE a2a_task_event_type AS ENUM (
      'created',
      'blocked',
      'claimed',
      'started',
      'completed',
      'failed',
      'requeued',
      'quarantined',
      'canceled'
    );
  END IF;
END$$;

-- Tasks table
CREATE TABLE IF NOT EXISTS public.a2a_tasks (
  task_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite_id uuid NOT NULL,
  created_by_office_id uuid NULL,

  assigned_to_agent text NOT NULL,
  task_type text NOT NULL,

  status a2a_task_status NOT NULL DEFAULT 'created',
  priority int NOT NULL DEFAULT 50,

  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  idempotency_key text NULL,
  correlation_id text NOT NULL DEFAULT gen_random_uuid()::text,

  requires_approval boolean NOT NULL DEFAULT false,
  approval_id uuid NULL,

  claimed_by text NULL,
  claimed_at timestamptz NULL,
  lease_expires_at timestamptz NULL,

  attempt_count int NOT NULL DEFAULT 0,
  last_error text NULL,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz NULL,

  CONSTRAINT a2a_blocked_requires_approval CHECK (status <> 'blocked' OR requires_approval = true)
);

-- Idempotency uniqueness (only when present)
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_tasks_suite_idempotency
  ON public.a2a_tasks (suite_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- Queue scan index
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_suite_agent_status_pri
  ON public.a2a_tasks (suite_id, assigned_to_agent, status, priority DESC, created_at ASC);

-- Event history
CREATE TABLE IF NOT EXISTS public.a2a_task_events (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL REFERENCES public.a2a_tasks(task_id) ON DELETE CASCADE,
  suite_id uuid NOT NULL,

  event_type a2a_task_event_type NOT NULL,
  actor_type text NOT NULL DEFAULT 'system',
  actor_id text NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_a2a_task_events_task_time
  ON public.a2a_task_events (task_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_a2a_task_events_suite_time
  ON public.a2a_task_events (suite_id, created_at DESC);

-- Updated_at trigger
CREATE OR REPLACE FUNCTION app.tg_touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_a2a_tasks_touch_updated_at ON public.a2a_tasks;
CREATE TRIGGER trg_a2a_tasks_touch_updated_at
BEFORE UPDATE ON public.a2a_tasks
FOR EACH ROW
EXECUTE FUNCTION app.tg_touch_updated_at();

-- Append-only event helper
CREATE OR REPLACE FUNCTION app.append_a2a_task_event(
  p_task_id uuid,
  p_suite_id uuid,
  p_event_type a2a_task_event_type,
  p_actor_type text DEFAULT 'system',
  p_actor_id text DEFAULT NULL,
  p_details jsonb DEFAULT '{}'::jsonb
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_event_id uuid;
BEGIN
  INSERT INTO public.a2a_task_events(task_id, suite_id, event_type, actor_type, actor_id, details)
  VALUES (p_task_id, p_suite_id, p_event_type, p_actor_type, p_actor_id, COALESCE(p_details, '{}'::jsonb))
  RETURNING event_id INTO v_event_id;

  RETURN v_event_id;
END;
$$;

-- Create 'created' event on task insert
CREATE OR REPLACE FUNCTION app.tg_a2a_tasks_on_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM app.append_a2a_task_event(
    NEW.task_id,
    NEW.suite_id,
    'created',
    'system',
    NULL,
    jsonb_build_object('assigned_to_agent', NEW.assigned_to_agent, 'task_type', NEW.task_type)
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_a2a_tasks_on_insert ON public.a2a_tasks;
CREATE TRIGGER trg_a2a_tasks_on_insert
AFTER INSERT ON public.a2a_tasks
FOR EACH ROW
EXECUTE FUNCTION app.tg_a2a_tasks_on_insert();

COMMIT;

-- ========== Migration: 20260116020100_a2a_inbox_rpcs.sql ==========
-- Trust Spine: Internal A2A Inbox MVP (RPCs)

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- Claim tasks (concurrency-safe). Only tasks that are not blocked by approval.
CREATE OR REPLACE FUNCTION app.claim_a2a_tasks(
  p_suite_id uuid,
  p_assigned_to_agent text,
  p_limit int DEFAULT 10,
  p_lease_seconds int DEFAULT 300,
  p_claimed_by text DEFAULT NULL
)
RETURNS SETOF public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_now timestamptz := now();
BEGIN
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  RETURN QUERY
  WITH candidates AS (
    SELECT t.task_id
    FROM public.a2a_tasks t
    WHERE t.suite_id = p_suite_id
      AND t.assigned_to_agent = p_assigned_to_agent
      AND t.status IN ('created', 'failed')
      AND t.requires_approval = false
      AND (t.status <> 'failed' OR (t.lease_expires_at IS NULL OR t.lease_expires_at <= v_now))
    ORDER BY t.priority DESC, t.created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT GREATEST(1, LEAST(p_limit, 100))
  ), updated AS (
    UPDATE public.a2a_tasks t
    SET status = 'claimed',
        claimed_at = v_now,
        claimed_by = COALESCE(p_claimed_by, p_assigned_to_agent),
        lease_expires_at = v_now + make_interval(secs => GREATEST(30, LEAST(p_lease_seconds, 3600))),
        attempt_count = attempt_count + 1
    WHERE t.task_id IN (SELECT task_id FROM candidates)
    RETURNING t.*
  )
  SELECT u.* FROM updated u;

  -- Append events
  INSERT INTO public.a2a_task_events(task_id, suite_id, event_type, actor_type, actor_id, details)
  SELECT u.task_id, u.suite_id, 'claimed', 'agent', COALESCE(p_claimed_by, p_assigned_to_agent),
         jsonb_build_object('lease_expires_at', u.lease_expires_at)
  FROM public.a2a_tasks u
  WHERE u.task_id IN (SELECT task_id FROM candidates);

END;
$$;

-- Mark task in progress (optional)
CREATE OR REPLACE FUNCTION app.start_a2a_task(
  p_task_id uuid,
  p_suite_id uuid,
  p_actor_id text DEFAULT 'agent'
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
BEGIN
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  UPDATE public.a2a_tasks
  SET status = 'in_progress'
  WHERE task_id = p_task_id
    AND suite_id = p_suite_id
    AND status IN ('claimed', 'created')
  RETURNING * INTO v_row;

  IF v_row.task_id IS NULL THEN
    RAISE EXCEPTION 'Task not found or invalid state';
  END IF;

  PERFORM app.append_a2a_task_event(p_task_id, p_suite_id, 'started', 'agent', p_actor_id, '{}'::jsonb);
  RETURN v_row;
END;
$$;

-- Complete task
CREATE OR REPLACE FUNCTION app.complete_a2a_task(
  p_task_id uuid,
  p_suite_id uuid,
  p_actor_id text DEFAULT 'agent',
  p_details jsonb DEFAULT '{}'::jsonb
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
BEGIN
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  UPDATE public.a2a_tasks
  SET status = 'done',
      completed_at = now(),
      lease_expires_at = NULL
  WHERE task_id = p_task_id
    AND suite_id = p_suite_id
    AND status IN ('claimed', 'in_progress')
  RETURNING * INTO v_row;

  IF v_row.task_id IS NULL THEN
    RAISE EXCEPTION 'Task not found or invalid state';
  END IF;

  PERFORM app.append_a2a_task_event(p_task_id, p_suite_id, 'completed', 'agent', p_actor_id, COALESCE(p_details, '{}'::jsonb));
  RETURN v_row;
END;
$$;

-- Fail task
CREATE OR REPLACE FUNCTION app.fail_a2a_task(
  p_task_id uuid,
  p_suite_id uuid,
  p_actor_id text DEFAULT 'agent',
  p_error text DEFAULT NULL,
  p_details jsonb DEFAULT '{}'::jsonb
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
  v_details jsonb;
BEGIN
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  UPDATE public.a2a_tasks
  SET status = 'failed',
      last_error = p_error,
      lease_expires_at = now() + make_interval(secs => 60)
  WHERE task_id = p_task_id
    AND suite_id = p_suite_id
    AND status IN ('claimed', 'in_progress')
  RETURNING * INTO v_row;

  IF v_row.task_id IS NULL THEN
    RAISE EXCEPTION 'Task not found or invalid state';
  END IF;

  v_details := COALESCE(p_details, '{}'::jsonb) || jsonb_build_object('error', p_error);
  PERFORM app.append_a2a_task_event(p_task_id, p_suite_id, 'failed', 'agent', p_actor_id, v_details);
  RETURN v_row;
END;
$$;

COMMIT;

-- ========== Migration: 20260116020200_a2a_inbox_rls.sql ==========
-- Trust Spine: Internal A2A Inbox MVP (RLS)
-- Suite-scoped access via current_setting('app.current_suite_id')::uuid

BEGIN;

ALTER TABLE public.a2a_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.a2a_task_events ENABLE ROW LEVEL SECURITY;

-- Read tasks within current suite
DROP POLICY IF EXISTS a2a_tasks_select_in_suite ON public.a2a_tasks;
CREATE POLICY a2a_tasks_select_in_suite
  ON public.a2a_tasks
  FOR SELECT
  TO authenticated
  USING (suite_id = current_setting('app.current_suite_id')::uuid);

-- Insert tasks within current suite
DROP POLICY IF EXISTS a2a_tasks_insert_in_suite ON public.a2a_tasks;
CREATE POLICY a2a_tasks_insert_in_suite
  ON public.a2a_tasks
  FOR INSERT
  TO authenticated
  WITH CHECK (suite_id = current_setting('app.current_suite_id')::uuid);

-- Update tasks within current suite
DROP POLICY IF EXISTS a2a_tasks_update_in_suite ON public.a2a_tasks;
CREATE POLICY a2a_tasks_update_in_suite
  ON public.a2a_tasks
  FOR UPDATE
  TO authenticated
  USING (suite_id = current_setting('app.current_suite_id')::uuid)
  WITH CHECK (suite_id = current_setting('app.current_suite_id')::uuid);

-- Events: read within suite
DROP POLICY IF EXISTS a2a_task_events_select_in_suite ON public.a2a_task_events;
CREATE POLICY a2a_task_events_select_in_suite
  ON public.a2a_task_events
  FOR SELECT
  TO authenticated
  USING (suite_id = current_setting('app.current_suite_id')::uuid);

-- Events: insert within suite
DROP POLICY IF EXISTS a2a_task_events_insert_in_suite ON public.a2a_task_events;
CREATE POLICY a2a_task_events_insert_in_suite
  ON public.a2a_task_events
  FOR INSERT
  TO authenticated
  WITH CHECK (suite_id = current_setting('app.current_suite_id')::uuid);

-- Recommended: prevent deletes
REVOKE DELETE ON public.a2a_tasks FROM authenticated;
REVOKE DELETE ON public.a2a_task_events FROM authenticated;

COMMIT;

-- ========== Migration: 20260116020300_a2a_inbox_hardening.sql ==========
-- Trust Spine: Internal A2A Inbox (Hardening Upgrade)
--
-- Adds:
--   1) Membership-based tenancy (app.suite_members) and suite-context authorization
--   2) RPC authorization checks (do not trust client-provided suite_id alone)
--   3) Lease-expiry reclaim for stuck claimed/in_progress tasks
--   4) Event suite_id consistency (suite_id derived from parent task)
--
-- Notes:
--   - This assumes Supabase-style JWT helpers (auth.uid()/auth.role()).
--     Wrappers below fall back to request.jwt.claim.* settings if auth.* is unavailable.

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- UUID generator (used by default values in the core migration)
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Membership table (minimum viable authorization primitive)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.suite_members (
  suite_id uuid NOT NULL,
  user_id uuid NOT NULL,
  role text NOT NULL DEFAULT 'member',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (suite_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_suite_members_user
  ON app.suite_members (user_id, suite_id);

-- ---------------------------------------------------------------------------
-- JWT helper wrappers (avoid hard dependency on auth.* at migration time)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.jwt_role()
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_role text;
BEGIN
  v_role := NULL;

  BEGIN
    EXECUTE 'SELECT auth.role()' INTO v_role;
  EXCEPTION WHEN undefined_function THEN
    v_role := NULL;
  END;

  IF v_role IS NULL OR v_role = '' THEN
    v_role := NULLIF(current_setting('request.jwt.claim.role', true), '');
  END IF;

  RETURN v_role;
END;
$$;

CREATE OR REPLACE FUNCTION app.jwt_uid()
RETURNS uuid
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_uid uuid;
  v_sub text;
BEGIN
  v_uid := NULL;

  BEGIN
    EXECUTE 'SELECT auth.uid()' INTO v_uid;
  EXCEPTION WHEN undefined_function THEN
    v_uid := NULL;
  END;

  IF v_uid IS NULL THEN
    v_sub := NULLIF(current_setting('request.jwt.claim.sub', true), '');
    IF v_sub IS NOT NULL THEN
      BEGIN
        v_uid := v_sub::uuid;
      EXCEPTION WHEN others THEN
        v_uid := NULL;
      END;
    END IF;
  END IF;

  RETURN v_uid;
END;
$$;

-- ---------------------------------------------------------------------------
-- Authorization guard
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.assert_suite_member(p_suite_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_role text;
  v_uid uuid;
BEGIN
  v_role := app.jwt_role();

  -- Service role / admin bypass (common in server-to-server jobs)
  IF v_role IN ('service_role', 'supabase_admin') OR current_user IN ('postgres') THEN
    RETURN;
  END IF;

  v_uid := app.jwt_uid();
  IF v_uid IS NULL THEN
    RAISE EXCEPTION 'not authorized: missing user identity';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM app.suite_members m
    WHERE m.suite_id = p_suite_id
      AND m.user_id = v_uid
  ) THEN
    RAISE EXCEPTION 'not authorized for suite %', p_suite_id;
  END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- Suite context helper (now enforces membership)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.set_suite_context(p_suite_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);
END;
$$;

-- ---------------------------------------------------------------------------
-- Event suite_id consistency (suite_id must always match parent task)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.tg_a2a_task_events_set_suite()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, app
AS $$
DECLARE
  v_suite_id uuid;
BEGIN
  SELECT t.suite_id INTO v_suite_id
  FROM public.a2a_tasks t
  WHERE t.task_id = NEW.task_id;

  IF v_suite_id IS NULL THEN
    RAISE EXCEPTION 'invalid task_id for event: %', NEW.task_id;
  END IF;

  NEW.suite_id := v_suite_id;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_a2a_task_events_set_suite ON public.a2a_task_events;
CREATE TRIGGER trg_a2a_task_events_set_suite
BEFORE INSERT ON public.a2a_task_events
FOR EACH ROW
EXECUTE FUNCTION app.tg_a2a_task_events_set_suite();

-- Backfill any existing mismatched suite_ids
UPDATE public.a2a_task_events e
SET suite_id = t.suite_id
FROM public.a2a_tasks t
WHERE e.task_id = t.task_id
  AND e.suite_id IS DISTINCT FROM t.suite_id;

-- Lease reclaim support index
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_suite_agent_lease
  ON public.a2a_tasks (suite_id, assigned_to_agent, lease_expires_at)
  WHERE status IN ('claimed', 'in_progress', 'failed');

-- ---------------------------------------------------------------------------
-- RPCs: create + claim + state transitions (all assert membership)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.create_a2a_task(
  p_suite_id uuid,
  p_created_by_office_id uuid,
  p_assigned_to_agent text,
  p_task_type text,
  p_payload jsonb DEFAULT '{}'::jsonb,
  p_priority int DEFAULT 50,
  p_idempotency_key text DEFAULT NULL,
  p_requires_approval boolean DEFAULT false,
  p_approval_id uuid DEFAULT NULL
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  INSERT INTO public.a2a_tasks(
    suite_id,
    created_by_office_id,
    assigned_to_agent,
    task_type,
    payload,
    priority,
    idempotency_key,
    requires_approval,
    approval_id
  )
  VALUES (
    p_suite_id,
    p_created_by_office_id,
    p_assigned_to_agent,
    p_task_type,
    COALESCE(p_payload, '{}'::jsonb),
    COALESCE(p_priority, 50),
    p_idempotency_key,
    COALESCE(p_requires_approval, false),
    p_approval_id
  )
  RETURNING * INTO v_row;

  RETURN v_row;
END;
$$;

-- Claim tasks (concurrency-safe + lease-expiry reclaim).
CREATE OR REPLACE FUNCTION app.claim_a2a_tasks(
  p_suite_id uuid,
  p_assigned_to_agent text,
  p_limit int DEFAULT 10,
  p_lease_seconds int DEFAULT 300,
  p_claimed_by text DEFAULT NULL
)
RETURNS SETOF public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_now timestamptz := now();
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  RETURN QUERY
  WITH candidates AS (
    SELECT t.task_id
    FROM public.a2a_tasks t
    WHERE t.suite_id = p_suite_id
      AND t.assigned_to_agent = p_assigned_to_agent
      AND t.requires_approval = false
      AND (
        t.status IN ('created', 'failed')
        OR (
          t.status IN ('claimed', 'in_progress')
          AND t.lease_expires_at IS NOT NULL
          AND t.lease_expires_at <= v_now
        )
      )
    ORDER BY t.priority DESC, t.created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT GREATEST(1, LEAST(p_limit, 100))
  ), updated AS (
    UPDATE public.a2a_tasks t
    SET status = 'claimed',
        claimed_at = v_now,
        claimed_by = COALESCE(p_claimed_by, p_assigned_to_agent),
        lease_expires_at = v_now + make_interval(secs => GREATEST(30, LEAST(p_lease_seconds, 3600))),
        attempt_count = attempt_count + 1
    WHERE t.task_id IN (SELECT task_id FROM candidates)
    RETURNING t.*
  )
  SELECT u.* FROM updated u;

  -- Append events for claimed tasks (suite_id set by trigger)
  INSERT INTO public.a2a_task_events(task_id, suite_id, event_type, actor_type, actor_id, details)
  SELECT u.task_id, u.suite_id, 'claimed', 'agent', COALESCE(p_claimed_by, p_assigned_to_agent),
         jsonb_build_object('lease_expires_at', u.lease_expires_at)
  FROM updated u;

END;
$$;

CREATE OR REPLACE FUNCTION app.start_a2a_task(
  p_task_id uuid,
  p_suite_id uuid,
  p_actor_id text DEFAULT 'agent'
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  UPDATE public.a2a_tasks
  SET status = 'in_progress'
  WHERE task_id = p_task_id
    AND suite_id = p_suite_id
    AND status IN ('claimed', 'created')
  RETURNING * INTO v_row;

  IF v_row.task_id IS NULL THEN
    RAISE EXCEPTION 'Task not found or invalid state';
  END IF;

  PERFORM app.append_a2a_task_event(p_task_id, p_suite_id, 'started', 'agent', p_actor_id, '{}'::jsonb);
  RETURN v_row;
END;
$$;

CREATE OR REPLACE FUNCTION app.complete_a2a_task(
  p_task_id uuid,
  p_suite_id uuid,
  p_actor_id text DEFAULT 'agent',
  p_details jsonb DEFAULT '{}'::jsonb
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  UPDATE public.a2a_tasks
  SET status = 'done',
      completed_at = now(),
      lease_expires_at = NULL
  WHERE task_id = p_task_id
    AND suite_id = p_suite_id
    AND status IN ('claimed', 'in_progress')
  RETURNING * INTO v_row;

  IF v_row.task_id IS NULL THEN
    RAISE EXCEPTION 'Task not found or invalid state';
  END IF;

  PERFORM app.append_a2a_task_event(p_task_id, p_suite_id, 'completed', 'agent', p_actor_id, COALESCE(p_details, '{}'::jsonb));
  RETURN v_row;
END;
$$;

CREATE OR REPLACE FUNCTION app.fail_a2a_task(
  p_task_id uuid,
  p_suite_id uuid,
  p_actor_id text DEFAULT 'agent',
  p_error text DEFAULT NULL,
  p_details jsonb DEFAULT '{}'::jsonb
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
  v_details jsonb;
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  UPDATE public.a2a_tasks
  SET status = 'failed',
      last_error = p_error,
      lease_expires_at = now() + make_interval(secs => 60)
  WHERE task_id = p_task_id
    AND suite_id = p_suite_id
    AND status IN ('claimed', 'in_progress')
  RETURNING * INTO v_row;

  IF v_row.task_id IS NULL THEN
    RAISE EXCEPTION 'Task not found or invalid state';
  END IF;

  v_details := COALESCE(p_details, '{}'::jsonb) || jsonb_build_object('error', p_error);
  PERFORM app.append_a2a_task_event(p_task_id, p_suite_id, 'failed', 'agent', p_actor_id, v_details);
  RETURN v_row;
END;
$$;

-- ---------------------------------------------------------------------------
-- RLS: require suite context + membership (service_role bypass allowed)
-- ---------------------------------------------------------------------------
ALTER TABLE public.a2a_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.a2a_task_events ENABLE ROW LEVEL SECURITY;

-- Tasks SELECT
DROP POLICY IF EXISTS a2a_tasks_select_in_suite ON public.a2a_tasks;
CREATE POLICY a2a_tasks_select_in_suite
  ON public.a2a_tasks
  FOR SELECT
  TO authenticated
  USING (
    app.jwt_role() = 'service_role'
    OR (
      suite_id = current_setting('app.current_suite_id', true)::uuid
      AND EXISTS (
        SELECT 1 FROM app.suite_members m
        WHERE m.suite_id = a2a_tasks.suite_id
          AND m.user_id = app.jwt_uid()
      )
    )
  );

-- Tasks INSERT
DROP POLICY IF EXISTS a2a_tasks_insert_in_suite ON public.a2a_tasks;
CREATE POLICY a2a_tasks_insert_in_suite
  ON public.a2a_tasks
  FOR INSERT
  TO authenticated
  WITH CHECK (
    app.jwt_role() = 'service_role'
    OR (
      suite_id = current_setting('app.current_suite_id', true)::uuid
      AND EXISTS (
        SELECT 1 FROM app.suite_members m
        WHERE m.suite_id = a2a_tasks.suite_id
          AND m.user_id = app.jwt_uid()
      )
    )
  );

-- Tasks UPDATE
DROP POLICY IF EXISTS a2a_tasks_update_in_suite ON public.a2a_tasks;
CREATE POLICY a2a_tasks_update_in_suite
  ON public.a2a_tasks
  FOR UPDATE
  TO authenticated
  USING (
    app.jwt_role() = 'service_role'
    OR (
      suite_id = current_setting('app.current_suite_id', true)::uuid
      AND EXISTS (
        SELECT 1 FROM app.suite_members m
        WHERE m.suite_id = a2a_tasks.suite_id
          AND m.user_id = app.jwt_uid()
      )
    )
  )
  WITH CHECK (
    app.jwt_role() = 'service_role'
    OR (
      suite_id = current_setting('app.current_suite_id', true)::uuid
      AND EXISTS (
        SELECT 1 FROM app.suite_members m
        WHERE m.suite_id = a2a_tasks.suite_id
          AND m.user_id = app.jwt_uid()
      )
    )
  );

-- Events SELECT
DROP POLICY IF EXISTS a2a_task_events_select_in_suite ON public.a2a_task_events;
CREATE POLICY a2a_task_events_select_in_suite
  ON public.a2a_task_events
  FOR SELECT
  TO authenticated
  USING (
    app.jwt_role() = 'service_role'
    OR (
      suite_id = current_setting('app.current_suite_id', true)::uuid
      AND EXISTS (
        SELECT 1 FROM app.suite_members m
        WHERE m.suite_id = a2a_task_events.suite_id
          AND m.user_id = app.jwt_uid()
      )
    )
  );

-- Events INSERT
DROP POLICY IF EXISTS a2a_task_events_insert_in_suite ON public.a2a_task_events;
CREATE POLICY a2a_task_events_insert_in_suite
  ON public.a2a_task_events
  FOR INSERT
  TO authenticated
  WITH CHECK (
    app.jwt_role() = 'service_role'
    OR (
      suite_id = current_setting('app.current_suite_id', true)::uuid
      AND EXISTS (
        SELECT 1 FROM app.suite_members m
        WHERE m.suite_id = a2a_task_events.suite_id
          AND m.user_id = app.jwt_uid()
      )
    )
  );

-- Keep original delete restrictions
REVOKE DELETE ON public.a2a_tasks FROM authenticated;
REVOKE DELETE ON public.a2a_task_events FROM authenticated;

COMMIT;

-- ========== Migration: 20260116020400_a2a_inbox_offices.sql ==========
-- Trust Spine: Internal A2A Inbox (Office / Seat Model Upgrade)
--
-- Adds first-class "Office" seats under each Suite (business tenant), plus office-level
-- execution routing for tasks.
--
-- Canonical model:
--   * suite_id  = business/company tenant boundary
--   * office_id = seat / team member identity within a suite
--
-- Outcome:
--   * Each task stays suite-scoped, and can optionally be assigned to a specific office.
--   * RPCs enforce suite membership, and (when an office is supplied) enforce office membership.
--   * This is designed to align with your product model: users can own multiple suites (businesses),
--     and each suite contains multiple offices (team members), each with their own Ava/agents.

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- Offices (seats) + membership
-- ---------------------------------------------------------------------------

-- A2A addon: app.offices was already created by suite_office_identity migration.
-- Add missing columns needed by A2A (additive ALTER instead of conflicting CREATE).
ALTER TABLE app.offices ADD COLUMN IF NOT EXISTS office_number text;
ALTER TABLE app.offices ADD COLUMN IF NOT EXISTS display_name text;
ALTER TABLE app.offices ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;
ALTER TABLE app.offices ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

-- Add unique constraint only if office_number is populated (optional for A2A)
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'offices_suite_id_office_number_key'
  ) THEN
    -- office_number is nullable (not all offices use A2A numbering), so skip unique constraint
    -- to avoid NOT NULL violations on existing rows. A2A consumers should enforce uniqueness at app level.
    NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_offices_suite
  ON app.offices (suite_id) WHERE office_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS app.office_members (
  office_id uuid NOT NULL REFERENCES app.offices(office_id) ON DELETE CASCADE,
  user_id uuid NOT NULL,
  role text NOT NULL DEFAULT 'member',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (office_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_office_members_user
  ON app.office_members (user_id, office_id);

-- Touch updated_at for offices
CREATE OR REPLACE FUNCTION app.tg_touch_updated_at_offices()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_offices_touch_updated_at ON app.offices;
CREATE TRIGGER trg_offices_touch_updated_at
BEFORE UPDATE ON app.offices
FOR EACH ROW
EXECUTE FUNCTION app.tg_touch_updated_at_offices();

-- ---------------------------------------------------------------------------
-- Authorization helpers
--   - app.assert_suite_member already exists (from hardening migration)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.assert_office_in_suite(
  p_suite_id uuid,
  p_office_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_role text;
BEGIN
  v_role := app.jwt_role();
  IF v_role IN ('service_role', 'supabase_admin') OR current_user IN ('postgres') THEN
    RETURN;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM app.offices o
    WHERE o.office_id = p_office_id
      AND o.suite_id = p_suite_id
      AND o.is_active = true
  ) THEN
    RAISE EXCEPTION 'invalid office % for suite %', p_office_id, p_suite_id;
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION app.assert_office_member(
  p_suite_id uuid,
  p_office_id uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_role text;
  v_uid uuid;
BEGIN
  v_role := app.jwt_role();

  -- Service role / admin bypass
  IF v_role IN ('service_role', 'supabase_admin') OR current_user IN ('postgres') THEN
    RETURN;
  END IF;

  -- Must be a suite member first
  PERFORM app.assert_suite_member(p_suite_id);

  -- Office must belong to the suite
  PERFORM app.assert_office_in_suite(p_suite_id, p_office_id);

  v_uid := app.jwt_uid();
  IF v_uid IS NULL THEN
    RAISE EXCEPTION 'not authorized: missing user identity';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM app.office_members om
    WHERE om.office_id = p_office_id
      AND om.user_id = v_uid
  ) THEN
    RAISE EXCEPTION 'not authorized for office %', p_office_id;
  END IF;
END;
$$;

-- Optional: store office context (parallel to suite context)
CREATE OR REPLACE FUNCTION app.set_office_context(p_office_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
BEGIN
  PERFORM set_config('app.current_office_id', p_office_id::text, true);
END;
$$;

-- ---------------------------------------------------------------------------
-- Tasks: office routing
-- ---------------------------------------------------------------------------

ALTER TABLE public.a2a_tasks
  ADD COLUMN IF NOT EXISTS assigned_to_office_id uuid NULL;

-- Optional soft FKs (NOT VALID to avoid breaking existing rows)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_a2a_tasks_created_by_office'
  ) THEN
    ALTER TABLE public.a2a_tasks
      ADD CONSTRAINT fk_a2a_tasks_created_by_office
      FOREIGN KEY (created_by_office_id)
      REFERENCES app.offices(office_id)
      ON DELETE SET NULL
      NOT VALID;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_a2a_tasks_assigned_to_office'
  ) THEN
    ALTER TABLE public.a2a_tasks
      ADD CONSTRAINT fk_a2a_tasks_assigned_to_office
      FOREIGN KEY (assigned_to_office_id)
      REFERENCES app.offices(office_id)
      ON DELETE SET NULL
      NOT VALID;
  END IF;
END;
$$;

-- Validate office ids belong to the same suite (enforced on new writes)
CREATE OR REPLACE FUNCTION app.tg_a2a_tasks_validate_offices()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, app
AS $$
BEGIN
  IF NEW.created_by_office_id IS NOT NULL THEN
    IF NOT EXISTS (
      SELECT 1 FROM app.offices o
      WHERE o.office_id = NEW.created_by_office_id
        AND o.suite_id = NEW.suite_id
        AND o.is_active = true
    ) THEN
      RAISE EXCEPTION 'created_by_office_id % is not in suite %', NEW.created_by_office_id, NEW.suite_id;
    END IF;
  END IF;

  IF NEW.assigned_to_office_id IS NOT NULL THEN
    IF NOT EXISTS (
      SELECT 1 FROM app.offices o
      WHERE o.office_id = NEW.assigned_to_office_id
        AND o.suite_id = NEW.suite_id
        AND o.is_active = true
    ) THEN
      RAISE EXCEPTION 'assigned_to_office_id % is not in suite %', NEW.assigned_to_office_id, NEW.suite_id;
    END IF;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_a2a_tasks_validate_offices ON public.a2a_tasks;
CREATE TRIGGER trg_a2a_tasks_validate_offices
BEFORE INSERT OR UPDATE ON public.a2a_tasks
FOR EACH ROW
EXECUTE FUNCTION app.tg_a2a_tasks_validate_offices();

-- Queue scan index including office routing
CREATE INDEX IF NOT EXISTS idx_a2a_tasks_suite_agent_office_status_pri
  ON public.a2a_tasks (suite_id, assigned_to_agent, assigned_to_office_id, status, priority DESC, created_at ASC);

-- ---------------------------------------------------------------------------
-- RPCs: re-define create + claim + transitions to support office identity
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.create_a2a_task(
  p_suite_id uuid,
  p_created_by_office_id uuid,
  p_assigned_to_agent text,
  p_task_type text,
  p_payload jsonb DEFAULT '{}'::jsonb,
  p_priority int DEFAULT 50,
  p_idempotency_key text DEFAULT NULL,
  p_requires_approval boolean DEFAULT false,
  p_approval_id uuid DEFAULT NULL,
  p_assigned_to_office_id uuid DEFAULT NULL
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  -- If a creator office is provided, enforce that the caller can act as that office.
  IF p_created_by_office_id IS NOT NULL THEN
    PERFORM app.assert_office_member(p_suite_id, p_created_by_office_id);
    PERFORM app.set_office_context(p_created_by_office_id);
  END IF;

  -- If assignment targets a specific office, ensure it belongs to the suite.
  IF p_assigned_to_office_id IS NOT NULL THEN
    PERFORM app.assert_office_in_suite(p_suite_id, p_assigned_to_office_id);
  END IF;

  INSERT INTO public.a2a_tasks(
    suite_id,
    created_by_office_id,
    assigned_to_office_id,
    assigned_to_agent,
    task_type,
    payload,
    priority,
    idempotency_key,
    requires_approval,
    approval_id
  )
  VALUES (
    p_suite_id,
    p_created_by_office_id,
    p_assigned_to_office_id,
    p_assigned_to_agent,
    p_task_type,
    COALESCE(p_payload, '{}'::jsonb),
    COALESCE(p_priority, 50),
    p_idempotency_key,
    COALESCE(p_requires_approval, false),
    p_approval_id
  )
  RETURNING * INTO v_row;

  RETURN v_row;
END;
$$;

CREATE OR REPLACE FUNCTION app.claim_a2a_tasks(
  p_suite_id uuid,
  p_assigned_to_agent text,
  p_limit int DEFAULT 10,
  p_lease_seconds int DEFAULT 300,
  p_claimed_by text DEFAULT NULL,
  p_actor_office_id uuid DEFAULT NULL,
  p_include_shared boolean DEFAULT true
)
RETURNS SETOF public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_now timestamptz := now();
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  IF p_actor_office_id IS NOT NULL THEN
    PERFORM app.assert_office_member(p_suite_id, p_actor_office_id);
    PERFORM app.set_office_context(p_actor_office_id);
  END IF;

  RETURN QUERY
  WITH candidates AS (
    SELECT t.task_id
    FROM public.a2a_tasks t
    WHERE t.suite_id = p_suite_id
      AND t.assigned_to_agent = p_assigned_to_agent
      AND t.requires_approval = false
      AND (
        -- office routing: if actor_office_id is null, only claim shared (unassigned) tasks
        (p_actor_office_id IS NULL AND t.assigned_to_office_id IS NULL)
        OR
        -- if actor_office_id is present, claim tasks assigned to that office, plus optionally shared
        (p_actor_office_id IS NOT NULL AND (
          t.assigned_to_office_id = p_actor_office_id
          OR (p_include_shared AND t.assigned_to_office_id IS NULL)
        ))
      )
      AND (
        t.status IN ('created', 'failed')
        OR (
          t.status IN ('claimed', 'in_progress')
          AND t.lease_expires_at IS NOT NULL
          AND t.lease_expires_at <= v_now
        )
      )
    ORDER BY t.priority DESC, t.created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT GREATEST(1, LEAST(p_limit, 100))
  ), updated AS (
    UPDATE public.a2a_tasks t
    SET status = 'claimed',
        claimed_at = v_now,
        claimed_by = COALESCE(p_claimed_by, p_assigned_to_agent),
        lease_expires_at = v_now + make_interval(secs => GREATEST(30, LEAST(p_lease_seconds, 3600))),
        attempt_count = attempt_count + 1
    WHERE t.task_id IN (SELECT task_id FROM candidates)
    RETURNING t.*
  )
  SELECT u.* FROM updated u;

  -- Append events for claimed tasks (suite_id set by trigger)
  INSERT INTO public.a2a_task_events(task_id, suite_id, event_type, actor_type, actor_id, details)
  SELECT u.task_id,
         u.suite_id,
         'claimed',
         'agent',
         COALESCE(p_claimed_by, p_assigned_to_agent),
         jsonb_build_object(
           'lease_expires_at', u.lease_expires_at,
           'actor_office_id', p_actor_office_id,
           'assigned_to_office_id', u.assigned_to_office_id
         )
  FROM updated u;

END;
$$;

CREATE OR REPLACE FUNCTION app.start_a2a_task(
  p_task_id uuid,
  p_suite_id uuid,
  p_actor_id text DEFAULT 'agent',
  p_actor_office_id uuid DEFAULT NULL
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  IF p_actor_office_id IS NOT NULL THEN
    PERFORM app.assert_office_member(p_suite_id, p_actor_office_id);
    PERFORM app.set_office_context(p_actor_office_id);
  END IF;

  UPDATE public.a2a_tasks
  SET status = 'in_progress'
  WHERE task_id = p_task_id
    AND suite_id = p_suite_id
    AND status IN ('claimed', 'created')
  RETURNING * INTO v_row;

  IF v_row.task_id IS NULL THEN
    RAISE EXCEPTION 'Task not found or invalid state';
  END IF;

  PERFORM app.append_a2a_task_event(
    p_task_id,
    p_suite_id,
    'started',
    'agent',
    p_actor_id,
    jsonb_build_object('actor_office_id', p_actor_office_id, 'assigned_to_office_id', v_row.assigned_to_office_id)
  );

  RETURN v_row;
END;
$$;

CREATE OR REPLACE FUNCTION app.complete_a2a_task(
  p_task_id uuid,
  p_suite_id uuid,
  p_actor_id text DEFAULT 'agent',
  p_details jsonb DEFAULT '{}'::jsonb,
  p_actor_office_id uuid DEFAULT NULL
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
  v_details jsonb;
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  IF p_actor_office_id IS NOT NULL THEN
    PERFORM app.assert_office_member(p_suite_id, p_actor_office_id);
    PERFORM app.set_office_context(p_actor_office_id);
  END IF;

  UPDATE public.a2a_tasks
  SET status = 'done',
      completed_at = now(),
      lease_expires_at = NULL
  WHERE task_id = p_task_id
    AND suite_id = p_suite_id
    AND status IN ('claimed', 'in_progress')
  RETURNING * INTO v_row;

  IF v_row.task_id IS NULL THEN
    RAISE EXCEPTION 'Task not found or invalid state';
  END IF;

  v_details := COALESCE(p_details, '{}'::jsonb)
    || jsonb_build_object('actor_office_id', p_actor_office_id, 'assigned_to_office_id', v_row.assigned_to_office_id);

  PERFORM app.append_a2a_task_event(p_task_id, p_suite_id, 'completed', 'agent', p_actor_id, v_details);
  RETURN v_row;
END;
$$;

CREATE OR REPLACE FUNCTION app.fail_a2a_task(
  p_task_id uuid,
  p_suite_id uuid,
  p_actor_id text DEFAULT 'agent',
  p_error text DEFAULT NULL,
  p_details jsonb DEFAULT '{}'::jsonb,
  p_actor_office_id uuid DEFAULT NULL
)
RETURNS public.a2a_tasks
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_row public.a2a_tasks;
  v_details jsonb;
BEGIN
  PERFORM app.assert_suite_member(p_suite_id);
  PERFORM set_config('app.current_suite_id', p_suite_id::text, true);

  IF p_actor_office_id IS NOT NULL THEN
    PERFORM app.assert_office_member(p_suite_id, p_actor_office_id);
    PERFORM app.set_office_context(p_actor_office_id);
  END IF;

  UPDATE public.a2a_tasks
  SET status = 'failed',
      last_error = p_error,
      lease_expires_at = now() + make_interval(secs => 60)
  WHERE task_id = p_task_id
    AND suite_id = p_suite_id
    AND status IN ('claimed', 'in_progress')
  RETURNING * INTO v_row;

  IF v_row.task_id IS NULL THEN
    RAISE EXCEPTION 'Task not found or invalid state';
  END IF;

  v_details := COALESCE(p_details, '{}'::jsonb)
    || jsonb_build_object('error', p_error, 'actor_office_id', p_actor_office_id, 'assigned_to_office_id', v_row.assigned_to_office_id);

  PERFORM app.append_a2a_task_event(p_task_id, p_suite_id, 'failed', 'agent', p_actor_id, v_details);
  RETURN v_row;
END;
$$;

-- ---------------------------------------------------------------------------
-- RLS for offices + office_members (minimal, production-safe default)
--   - suite_members controls tenant access
--   - office_members controls seat access
-- ---------------------------------------------------------------------------

ALTER TABLE app.offices ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.office_members ENABLE ROW LEVEL SECURITY;

-- Offices SELECT: allow suite members to read offices in-suite
DROP POLICY IF EXISTS offices_select_in_suite ON app.offices;
CREATE POLICY offices_select_in_suite
  ON app.offices
  FOR SELECT
  TO authenticated
  USING (
    app.jwt_role() = 'service_role'
    OR EXISTS (
      SELECT 1 FROM app.suite_members sm
      WHERE sm.suite_id = offices.suite_id
        AND sm.user_id = app.jwt_uid()
    )
  );

-- Offices INSERT/UPDATE: default to service_role only (admin controlled)
REVOKE INSERT, UPDATE, DELETE ON app.offices FROM authenticated;

-- Office members: allow a user to see their own office memberships
DROP POLICY IF EXISTS office_members_select_self ON app.office_members;
CREATE POLICY office_members_select_self
  ON app.office_members
  FOR SELECT
  TO authenticated
  USING (
    app.jwt_role() = 'service_role'
    OR office_members.user_id = app.jwt_uid()
  );

REVOKE INSERT, UPDATE, DELETE ON app.office_members FROM authenticated;

COMMIT;

-- ========== Migration: 20260116020500_a2a_receipts_bridge.sql ==========
-- A2A receipts bridge (Option B)
-- Writes A2A message activity into the canonical receipts ledger.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Create a receipt for a single message.
CREATE OR REPLACE FUNCTION public.a2a_write_message_receipt(
  p_suite_id uuid,
  p_office_id uuid,
  p_message_id text,
  p_thread_id text,
  p_from_agent text,
  p_to_agent text,
  p_subject text,
  p_payload jsonb,
  p_correlation_id text DEFAULT NULL
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, app
AS $$
DECLARE
  v_receipt_id text;
BEGIN
  INSERT INTO receipts(
    suite_id,
    office_id,
    receipt_type,
    status,
    correlation_id,
    actor_type,
    actor_id,
    action,
    result
  ) VALUES (
    p_suite_id,
    p_office_id,
    'A2A_MESSAGE',
    'SUCCESS',
    COALESCE(p_correlation_id, p_message_id),
    'AGENT',
    p_from_agent,
    jsonb_build_object(
      'message_id', p_message_id,
      'thread_id', p_thread_id,
      'from_agent', p_from_agent,
      'to_agent', p_to_agent,
      'subject', p_subject,
      'payload', p_payload
    ),
    '{}'::jsonb
  ) RETURNING receipt_id INTO v_receipt_id;

  RETURN v_receipt_id;
END;
$$;

REVOKE ALL ON FUNCTION public.a2a_write_message_receipt(uuid,uuid,text,text,text,text,text,jsonb,text) FROM public;

-- ========== Migration: 20260116020600_a2a_suites_tenant_bridge.sql ==========
-- A2A Inbox v6: Suites (business tenants) + tenant_id bridge for Trust Spine
--
-- Aspire model:
--   * suite_id (uuid) = internal business tenant identifier
--   * tenant_id (text) = suite number (user-facing) / Trust Spine tenant key

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.suites (
  suite_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL UNIQUE,
  name text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_app_suites_tenant_id ON app.suites(tenant_id);

CREATE OR REPLACE FUNCTION app.ensure_suite(p_tenant_id text, p_name text DEFAULT NULL)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = app, public
AS $$
DECLARE
  v_suite_id uuid;
BEGIN
  IF p_tenant_id IS NULL OR btrim(p_tenant_id) = '' THEN
    RAISE EXCEPTION 'tenant_id required';
  END IF;

  SELECT suite_id INTO v_suite_id FROM app.suites WHERE tenant_id = p_tenant_id;

  IF v_suite_id IS NOT NULL THEN
    IF p_name IS NOT NULL THEN
      UPDATE app.suites SET name = COALESCE(name, p_name) WHERE suite_id = v_suite_id;
    END IF;
    RETURN v_suite_id;
  END IF;

  INSERT INTO app.suites(tenant_id, name)
  VALUES (p_tenant_id, p_name)
  RETURNING suite_id INTO v_suite_id;

  IF to_regclass('public.tenants') IS NOT NULL THEN
    INSERT INTO public.tenants(tenant_id, name)
    VALUES (p_tenant_id, COALESCE(p_name, p_tenant_id))
    ON CONFLICT (tenant_id) DO NOTHING;
  END IF;

  RETURN v_suite_id;
END;
$$;

REVOKE ALL ON FUNCTION app.ensure_suite(text, text) FROM public;

CREATE OR REPLACE FUNCTION app.suite_tenant_id(p_suite_id uuid)
RETURNS text
LANGUAGE sql
STABLE
SET search_path = app
AS $$
  SELECT tenant_id FROM app.suites WHERE suite_id = p_suite_id
$$;

REVOKE ALL ON FUNCTION app.suite_tenant_id(uuid) FROM public;

COMMIT;
