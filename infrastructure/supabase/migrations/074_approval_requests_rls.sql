-- Enable Row-Level Security on approval_requests
-- Required for Supabase Realtime subscriptions (Realtime requires RLS to filter by tenant)
-- Uses existing app.is_member(tenant_id) from Trust Spine (trust-spine-bundle.sql)
-- Law #6: Tenant Isolation — zero cross-tenant reads/writes

ALTER TABLE public.approval_requests ENABLE ROW LEVEL SECURITY;

-- Authenticated users can only see their own tenant's approval requests
CREATE POLICY approval_requests_select ON public.approval_requests
  FOR SELECT TO authenticated
  USING (app.is_member(tenant_id));

-- Service role bypasses RLS for backend operations (orchestrator writes)
CREATE POLICY approval_requests_service_role ON public.approval_requests
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- Grant SELECT to authenticated role (required for Realtime subscription)
GRANT SELECT ON public.approval_requests TO authenticated;
