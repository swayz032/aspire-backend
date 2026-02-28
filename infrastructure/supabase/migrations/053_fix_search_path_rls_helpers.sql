-- Migration 053: Fix mutable search_path on RLS helper functions
-- Resolves Supabase lint warnings:
--   "Function public.current_suite_id has a role mutable search_path"
--   "Function public.current_office_id has a role mutable search_path"
--
-- Setting search_path = '' prevents search_path hijacking attacks
-- where a malicious user creates objects in a higher-priority schema.
-- Already applied to production Supabase on 2026-02-16.

CREATE OR REPLACE FUNCTION public.current_suite_id()
  RETURNS uuid
  LANGUAGE sql
  STABLE
  SET search_path = ''
AS $$
  SELECT nullif(current_setting('app.current_suite_id', true), '')::uuid;
$$;

CREATE OR REPLACE FUNCTION public.current_office_id()
  RETURNS uuid
  LANGUAGE sql
  STABLE
  SET search_path = ''
AS $$
  SELECT nullif(current_setting('app.current_office_id', true), '')::uuid;
$$;
