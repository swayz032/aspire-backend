-- Migration 075: Add inviter_role to conference_invitations
-- Stores the inviter's role/title (e.g. "Owner") for display in the incoming call widget.
-- Additive nullable column — no data loss, backward compatible.

ALTER TABLE public.conference_invitations ADD COLUMN IF NOT EXISTS inviter_role TEXT;
