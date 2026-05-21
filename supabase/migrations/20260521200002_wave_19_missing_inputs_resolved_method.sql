-- Wave 19: add resolved_method column to blueprint_missing_inputs
-- resolved_by is uuid (auth.users FK) — for automated cascade resolution,
-- resolved_by stays NULL (no user actor) and resolved_method captures the
-- resolver method string (e.g., 'cascade:wave19:verifier_signals').
ALTER TABLE public.blueprint_missing_inputs
  ADD COLUMN IF NOT EXISTS resolved_method text;
