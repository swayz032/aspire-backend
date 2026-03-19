-- Platform Admin RLS Bypass
-- Allows users in admin_allowlist to see ALL data across all tenants
-- in the admin portal. Regular tenant isolation is preserved for non-admins.

-- 1. Create app.is_platform_admin() function
CREATE OR REPLACE FUNCTION app.is_platform_admin()
RETURNS boolean
LANGUAGE sql
STABLE SECURITY DEFINER
SET search_path TO 'public'
AS $$
  SELECT EXISTS (
    SELECT 1 FROM admin_allowlist a
    JOIN auth.users u ON u.email = a.email
    WHERE u.id = auth.uid()
  );
$$;

-- 2. Platform admin SELECT policies (read-only cross-tenant access)

-- Receipts (Incidents, Receipts, Activity pages)
CREATE POLICY receipts_select_platform_admin ON public.receipts
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Provider call log
CREATE POLICY provider_call_log_select_platform_admin ON public.provider_call_log
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Outbox jobs
CREATE POLICY outbox_jobs_select_platform_admin ON public.outbox_jobs
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Approval requests
CREATE POLICY approval_requests_select_platform_admin ON public.approval_requests
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Client events
CREATE POLICY client_events_select_platform_admin ON public.client_events
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Incidents
CREATE POLICY incidents_select_platform_admin ON public.incidents
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Tenant memberships (Customers page)
CREATE POLICY tenant_memberships_select_platform_admin ON public.tenant_memberships
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Workflow executions
CREATE POLICY workflow_executions_select_platform_admin ON public.workflow_executions
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Suite profiles (Customers page — cross-tenant profile data)
CREATE POLICY suite_profiles_select_platform_admin ON public.suite_profiles
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Agent registry (Agent Studio page)
CREATE POLICY agent_registry_select_platform_admin ON public.agent_registry
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Config proposals (Agent Studio config)
CREATE POLICY config_proposals_select_platform_admin ON public.config_proposals
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Config rollouts (Agent Studio rollouts)
CREATE POLICY config_rollouts_select_platform_admin ON public.config_rollouts
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Finance connections (Connected Apps page)
CREATE POLICY finance_connections_select_platform_admin ON public.finance_connections
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Finance events (Runway/Burn, Metrics pages)
CREATE POLICY finance_events_select_platform_admin ON public.finance_events
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Audit log (Auth audit trail)
CREATE POLICY audit_log_select_platform_admin ON public.audit_log
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Outbox dead letters (Dead Letter Queue page)
CREATE POLICY outbox_dead_letters_select_platform_admin ON public.outbox_dead_letters
  FOR SELECT TO authenticated
  USING (app.is_platform_admin());

-- Admin allowlist (self-referential: admins can read the allowlist)
CREATE POLICY admin_allowlist_select_authenticated ON public.admin_allowlist
  FOR SELECT TO authenticated
  USING (true);
