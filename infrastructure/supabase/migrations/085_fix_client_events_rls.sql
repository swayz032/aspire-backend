-- Migration 085: Fix client_events RLS — remove cross-tenant anonymous INSERT policies
-- Security fix: anonymous users could insert events for any tenant_id.
-- Now only authenticated users can insert, and only for their own tenant.

-- Drop vulnerable anonymous INSERT policies
DROP POLICY IF EXISTS "client_events_insert_anon" ON public.client_events;
DROP POLICY IF EXISTS "client_events_insert_anon_role" ON public.client_events;

-- Add tenant-scoped authenticated INSERT policy
CREATE POLICY "client_events_insert_authenticated"
  ON public.client_events
  FOR INSERT
  TO authenticated
  WITH CHECK (
    tenant_id IS NOT NULL
    AND app.is_member(tenant_id)
  );
