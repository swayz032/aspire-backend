-- Migration 072: Fix CRITICAL RLS misconfiguration on conference_invitations
-- The "Service role full access" policy targeted PUBLIC (all roles), which
-- inadvertently granted all authenticated/anon users INSERT/UPDATE/DELETE access.
-- Service role already has BYPASSRLS — the policy was redundant AND dangerous.
--
-- Fix: Drop the over-permissive policy, add scoped UPDATE policy for authenticated users.

DROP POLICY IF EXISTS "Service role full access" ON public.conference_invitations;

-- Users can only UPDATE their own invitations (accept/decline)
CREATE POLICY "Users update own invitations" ON public.conference_invitations
  FOR UPDATE
  TO authenticated
  USING (invitee_user_id = auth.uid())
  WITH CHECK (invitee_user_id = auth.uid());

-- No INSERT policy for authenticated — all inserts go through Express/service_role.
-- No DELETE policy — invitations are never deleted, only status-changed.
