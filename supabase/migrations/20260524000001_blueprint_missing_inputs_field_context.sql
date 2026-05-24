-- Wave 1.5b: Add field_context jsonb column to blueprint_missing_inputs
--
-- Pre-existing schema-drift bug — code at services/blueprint/supplier_matcher.py and
-- elsewhere writes field_context to blueprint_missing_inputs, but the column was
-- never added. Surfaced by wave-27-validation run on 2026-05-24 (~47 insert failures
-- with PGRST204 "Could not find the 'field_context' column").
--
-- field_context stores the surrounding evidence for a missing input:
-- - source_sheet_id
-- - source_region (legend|schedule|callout|annotation)
-- - source_excerpt (truncated OCR text per Law #9)
-- - confidence_basis

ALTER TABLE public.blueprint_missing_inputs
  ADD COLUMN IF NOT EXISTS field_context jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_bp_missing_inputs_field_context
  ON public.blueprint_missing_inputs USING gin (field_context);

COMMENT ON COLUMN public.blueprint_missing_inputs.field_context IS
  'Wave 1.5b: surrounding evidence for the missing input (sheet/region/excerpt/basis). NOT NULL with default {} so historical rows stay valid.';
