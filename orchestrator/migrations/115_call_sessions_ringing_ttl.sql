-- Migration 115: TTL cleanup for orphaned ringing call_sessions rows
--
-- Purpose: Prevent stale 'ringing' rows from re-appearing in the frontend
-- poll after the owner has already missed/dismissed the call. Any row that
-- has been in status='ringing' for more than 60 seconds without being
-- answered (status transition to 'in_progress') is flipped to 'failed'.
--
-- Risk Tier: GREEN — no data is deleted; status field is updated.
-- Idempotency: CREATE OR REPLACE + SELECT cron.schedule are both safe to
--   re-apply. The schedule upserts by name if 'expire_ringing_calls' already
--   exists (pg_cron behaviour).
--
-- pg_cron is enabled (confirmed via pg_extension: pg_cron 1.6.4).

-- UP -------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.expire_orphan_ringing_calls()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  UPDATE public.call_sessions
  SET
    status     = 'failed',
    ended_at   = NOW(),
    updated_at = NOW(),
    metadata   = COALESCE(metadata, '{}'::jsonb)
                 || '{"expired_reason": "ringing_timeout"}'::jsonb
  WHERE
    status     = 'ringing'
    AND started_at < NOW() - INTERVAL '60 seconds';
END;
$$;

COMMENT ON FUNCTION public.expire_orphan_ringing_calls() IS
  'Flips orphaned ringing call_sessions rows to failed after 60 s. '
  'Called by pg_cron every minute. Part of migration 115.';

-- Schedule via pg_cron (runs every minute, upserts by job name)
SELECT cron.schedule(
  'expire_ringing_calls',
  '* * * * *',
  $$SELECT public.expire_orphan_ringing_calls();$$
);

-- DOWN -----------------------------------------------------------------------
-- SELECT cron.unschedule('expire_ringing_calls');
-- DROP FUNCTION IF EXISTS public.expire_orphan_ringing_calls();
