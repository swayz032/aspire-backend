-- Migration 093: Add display_name to conference_join_codes
-- Stores the human-readable room label (e.g. "Suite A-1234 • Room CR-A1234")
-- so guest join pages can show the room identity instead of raw UUIDs.

ALTER TABLE public.conference_join_codes
  ADD COLUMN IF NOT EXISTS display_name TEXT;
