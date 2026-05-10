-- Migration 115: Make call_sessions.business_line_id nullable for EL compatibility
--
-- Bug fix: call_logger.py inserts EL post-call data into call_sessions but
-- call_sessions.business_line_id was NOT NULL with no default. EL calls route
-- via tenant_phone_numbers which carries no business_line_id FK, so every
-- EL post-call INSERT failed silently inside the enrichment try/except block.
-- No contacts or call sessions were ever persisted from receptionist calls.
--
-- This migration makes business_line_id nullable so EL calls can be logged
-- without a business_line association. LiveKit/direct calls that do have a
-- business_line_id continue to set it; EL calls leave it NULL.

ALTER TABLE public.call_sessions
  ALTER COLUMN business_line_id DROP NOT NULL;
