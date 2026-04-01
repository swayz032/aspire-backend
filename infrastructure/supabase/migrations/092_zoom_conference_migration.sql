-- Migration 092: Replace LiveKit references with Zoom Video SDK
-- Context: Aspire is migrating from LiveKit to Zoom Video SDK for video conferencing.
-- This migration updates the conference_invitations table to use Zoom session IDs.

-- Step 1: Add new column for Zoom session ID
ALTER TABLE public.conference_invitations
  ADD COLUMN IF NOT EXISTS zoom_session_id TEXT;

-- Step 2: Backfill from existing livekit_server_url (used as room identifier)
UPDATE public.conference_invitations
  SET zoom_session_id = livekit_server_url
  WHERE zoom_session_id IS NULL
    AND livekit_server_url IS NOT NULL;

-- Step 3: Drop the old LiveKit column
ALTER TABLE public.conference_invitations
  DROP COLUMN IF EXISTS livekit_server_url;

-- Step 4: Add index on zoom_session_id for efficient lookups
CREATE INDEX IF NOT EXISTS idx_conference_invitations_zoom_session_id
  ON public.conference_invitations (zoom_session_id)
  WHERE zoom_session_id IS NOT NULL;

COMMENT ON COLUMN public.conference_invitations.zoom_session_id IS
  'Zoom Video SDK session topic/ID for this conference invitation';
