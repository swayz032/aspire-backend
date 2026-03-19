-- Migration 086: Fix admin_allowlist + provider_call_log RLS cleanup
-- Security fix: any authenticated user could enumerate all platform admin emails.
-- Also defensively drop pcl_auth_select (already removed but ensuring migration idempotency).

-- admin_allowlist: drop the open SELECT policy
-- The scoped "Admins can view allowlist" policy with has_role(auth.uid(), 'admin') remains.
DROP POLICY IF EXISTS "admin_allowlist_select_authenticated" ON public.admin_allowlist;

-- provider_call_log: defensive cleanup
-- pcl_select_admin (scoped to app.is_admin_or_owner) is the correct policy.
DROP POLICY IF EXISTS "pcl_auth_select" ON public.provider_call_log;
