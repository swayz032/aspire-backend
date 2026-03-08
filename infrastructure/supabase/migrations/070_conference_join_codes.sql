-- Migration 070: Persistent conference join codes
-- Replaces in-memory Map that was wiped on every deploy.
-- Service role only — join codes are resolved server-side, no direct client access.

CREATE TABLE IF NOT EXISTS public.conference_join_codes (
  code TEXT PRIMARY KEY,
  token TEXT NOT NULL,
  room_name TEXT NOT NULL,
  guest_name TEXT NOT NULL,
  created_by TEXT NOT NULL,
  server_url TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for efficient expired code cleanup
CREATE INDEX idx_join_codes_expires ON public.conference_join_codes (expires_at);

-- RLS: deny all direct access (service role bypasses RLS)
ALTER TABLE public.conference_join_codes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role only" ON public.conference_join_codes
  USING (false) WITH CHECK (false);
