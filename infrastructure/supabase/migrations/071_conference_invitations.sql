-- Migration 071: Conference invitations for FaceTime-style video call notifications
-- Stores internal Aspire-to-Aspire conference invitations with realtime subscriptions.
-- RLS: users can see their own invitations; service role has full access.
-- Added to supabase_realtime publication for INSERT/UPDATE push to clients.

CREATE TABLE IF NOT EXISTS public.conference_invitations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  inviter_suite_id UUID NOT NULL,
  inviter_user_id UUID NOT NULL,
  inviter_name TEXT NOT NULL,
  inviter_avatar_url TEXT,
  inviter_suite_display_id TEXT NOT NULL,
  inviter_office_display_id TEXT NOT NULL,
  inviter_business_name TEXT,
  invitee_suite_id UUID NOT NULL,
  invitee_user_id UUID NOT NULL,
  room_name TEXT NOT NULL,
  livekit_server_url TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'accepted', 'declined', 'expired')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  responded_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '60 seconds'),
  CONSTRAINT fk_invitee FOREIGN KEY (invitee_suite_id)
    REFERENCES suite_profiles(suite_id)
);

-- Fast lookup: pending invitations for a specific user (filtered index)
CREATE INDEX idx_conf_inv_invitee ON public.conference_invitations
  (invitee_user_id, status) WHERE status = 'pending';

-- Cleanup: efficient expired invitation queries
CREATE INDEX idx_conf_inv_expires ON public.conference_invitations (expires_at);

-- RLS: users see only their own invitations
ALTER TABLE public.conference_invitations ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users see own invitations" ON public.conference_invitations
  FOR SELECT USING (invitee_user_id = auth.uid());
CREATE POLICY "Service role full access" ON public.conference_invitations
  FOR ALL USING (true) WITH CHECK (true);

-- Enable realtime for INSERT/UPDATE push to connected clients
ALTER PUBLICATION supabase_realtime ADD TABLE public.conference_invitations;

-- Required for Supabase Realtime filtered subscriptions (e.g. filter by invitee_user_id)
-- Without FULL, the WAL only includes primary key columns and filters cannot be evaluated
ALTER TABLE public.conference_invitations REPLICA IDENTITY FULL;
